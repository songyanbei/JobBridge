# 岗位生命周期 Phase 10 发布手册

本手册从 `backend` 目录执行命令。数据库连接、Redis 连接和对象存储配置必须指向同一套待发布环境；所有功能开关在发布开始前保持 `false`。

## 1. 发布前冻结

1. 确认待发布制品的 commit、镜像 digest 和回滚制品均已记录。
2. 确认 `JOB_REPLACEMENT_ENABLED=false`、`JOB_EXPIRY_CLEANUP_ENABLED=false`、`JOB_CANDIDATE_CLEANUP_ENABLED=false`、`JOB_HARD_DELETE_ENABLED=false`。在所有 API、消息 Worker、scheduler 和 session recovery Worker 上预先配置同一份 `RECOMMENDATION_CONTENT_KEY` 或 `RECOMMENDATION_CONTENT_KEY_RING`，并统一 `RECOMMENDATION_CONTENT_KEY_ACTIVE_VERSION`；密钥材料只能来自 secrets/KMS，不得写入日志或发布证据。
3. 停止全部 API、消息 Worker、scheduler 和 session recovery Worker，等待当前请求结束并确认旧进程全部退出。
4. 禁止新旧 API 或 Worker 混跑，禁止跨 003/004 schema 边界滚动发布。
5. 完成并校验 MySQL 全量备份。确认 Redis AOF 文件及持久化目录可恢复；不得清空 revocation fence。

旧 Worker 不写 session deadline 或 lease owner，会绕过绝对截止和 owner fencing。发现任何旧实例仍存活时，发布必须停止。

## 2. 迁移

为 MySQL 8 客户端准备权限为 `0600` 的配置文件，至少在 `[client]` 中配置待发布环境的 `host`、`port`、`user`、`password` 和 TLS 参数。配置文件不得提交到代码库。所有迁移和回滚必须复用同一个配置文件和数据库名；先输出实际数据库、主机和端口，人工核对后再执行：

```bash
export PHASE10_MYSQL_DEFAULTS_FILE=/run/secrets/jobbridge-mysql-client.cnf
test -r "$PHASE10_MYSQL_DEFAULTS_FILE"
PHASE10_MYSQL=(mysql --defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE" --database="$DB_NAME" --show-warnings)
"${PHASE10_MYSQL[@]}" --batch --skip-column-names -e "SELECT DATABASE(), @@hostname, @@port"
```

核验输出与变更单中的目标完全一致后，按固定顺序执行。任一命令失败立即停止，不得跳过后继续；每一步输出均归档为发布证据：

```bash
set -euo pipefail
test -r "$PHASE10_MYSQL_DEFAULTS_FILE"
PHASE10_MYSQL=(mysql --defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE" --database="$DB_NAME" --show-warnings)
"${PHASE10_MYSQL[@]}" --batch --skip-column-names -e "SELECT DATABASE(), @@hostname, @@port"
"${PHASE10_MYSQL[@]}" < sql/migrations/phase10_001_job_lifecycle_additive.sql | tee phase10-001-output.txt
# 归档上一条输出中的 backup rows/checksum，并核对 backup_rows=job_rows
"${PHASE10_MYSQL[@]}" < sql/migrations/phase10_002_media_dead_letter.sql | tee phase10-002-output.txt
"${PHASE10_MYSQL[@]}" < sql/migrations/phase10_003_session_commit_deadline.sql | tee phase10-003-output.txt
"${PHASE10_MYSQL[@]}" < sql/migrations/phase10_004_session_commit_lease_owner.sql | tee phase10-004-output.txt
```

003、004 必须在所有旧 Worker 停止后执行。迁移后不得启动旧版本进程。

## 3. 媒体回填

先 dry-run 并归档 CSV/JSON 报告，再 apply，最后重新 dry-run：

```bash
python -m scripts.backfill_media_lifecycle --output-dir phase10-media-dry-run
python -m scripts.backfill_media_lifecycle --apply --output-dir phase10-media-apply
python -m scripts.backfill_media_lifecycle --output-dir phase10-media-verify
```

最终报告中的 missing、repair-required、media delete dead-letter、invalid JSON、unresolved reference 和 conflict 阻断项必须全部为 0。`non_deleted_soft_deleted_media_key_count` 是历史硬删待处理量，不是硬删开关开启前的 blocker；回填只补齐 ownership coverage，不得把激活岗位或简历的媒体置为 `delete_pending`。第一次 dry-run 发现缺口时返回非零是预期行为，不能因此跳过 apply 后的复核。

## 4. Target cleanup 回填

Target cleanup 回填必须按 dry-run、apply、coverage 复核、完整 preflight 的顺序执行：

```bash
python -m scripts.backfill_target_cleanup_tasks
python -m scripts.backfill_target_cleanup_tasks --apply
python -m scripts.backfill_target_cleanup_tasks
# 上一步输出必须为 missing=0
python -m scripts.phase10_clock_check
python -m scripts.phase10_preflight
```

第一次 dry-run 发现缺失任务时会返回非零，这是预期的发布阻断信号。apply 完成后必须重新 dry-run；只有全局重查为 `missing=0` 才能继续。时钟检查使用 Redis `TIME` 包围 MySQL `UNIX_TIMESTAMP(NOW(6))` 取样，并把采样窗口计入保守偏差上界；该上界不得超过 2 秒。时钟检查或完整 preflight 非零、或 preflight 未返回 `ready=true`，均禁止部署。

## 5. 同版本部署

1. 构建一次制品，API、消息 Worker 和 scheduler 必须使用同一镜像 digest。
2. 保持四个岗位生命周期开关为 `false`，同时启动新版本 API、消息 Worker、scheduler 和 session recovery Worker；不得让旧版本回流。
3. 检查 `/health`、消息 Worker/scheduler/session recovery Worker heartbeat、各进程版本和镜像 digest、Redis durability policy、推荐正文活动密钥版本及数据库连接；四类进程必须与已记录的同一制品和同一配置版本一致，只核对密钥版本和可解析状态，不输出密钥材料。
4. 执行一轮不创建 replacement 的基础消息、后台查询和推荐发送 smoke test。
5. 再次执行 `python -m scripts.phase10_clock_check` 和 `python -m scripts.phase10_preflight`，结果必须通过。

## 6. 开关顺序

每一步至少观察一个完整 worker 调度周期；任一门禁或监控异常时停止，不得继续打开后续开关。

1. 先以待启用的相同配置执行 `JOB_REPLACEMENT_ENABLED=true python -m scripts.phase10_preflight`，确认 `recommendation_content_key_unavailable=0` 且 `ready=true`；再在所有 API、消息 Worker、scheduler 和 session recovery Worker 同步开启 `JOB_REPLACEMENT_ENABLED=true`，验证候选创建、审核、取消、激活和推荐消息投递。缺少活动版本密钥或各实例配置版本不一致时禁止开启。
2. 开启 `JOB_CANDIDATE_CLEANUP_ENABLED=true`，验证过期候选进入 media/target durable cleanup。
3. 开启 `JOB_EXPIRY_CLEANUP_ENABLED=true`，验证到期岗位软删除、version 增长和 cleanup task 创建。
4. 确认 coverage blocker 为 0、无 dead-letter 且 hard-delete 延迟窗口满足后，开启 `JOB_HARD_DELETE_ENABLED=true`。硬删任务负责把已超过延迟的历史媒体置为 `delete_pending`，并由逐行 media/target fail-closed 门禁阻止岗位提前物理删除；禁止由回填脚本代替该步骤。
5. 持续运行 hard-delete、media 和 target worker，直到 `non_deleted_soft_deleted_media_key_count=0`、media/target backlog 收敛且无 dead-letter，再次执行完整 preflight 并归档结果，才可结束发布窗口。

开关变更要求 API、Worker 和 scheduler 使用同一配置版本；禁止只重启部分实例造成行为混合。

## 7. 监控

发布窗口持续观察以下指标和结构化事件：

- `outbox_health`、`outbox_dead_letter`、`outbox_pending_age`。
- `session_commit_health`、`session_commit_pending_age`、session deadline terminal 数量。
- `media_cleanup_health`、`media_cleanup_dead_letter`、`delete_pending` 和 `dead_letter` 数量。
- `target_cleanup_task` 的 `pending`、`processing`、`retry_wait`、`dead_letter` 数量和最老任务年龄。
- `passed_without_activation`、`invalid_unactivated_candidate`、`active_replacement_graph_missing`、`soft_deleted_without_cleanup_task`。
- MySQL deadlock、lock wait timeout、Worker heartbeat、队列积压和 API 5xx。

每次开关变更后重新运行 `python -m scripts.phase10_preflight`。任一 blocker 非零、dead-letter 新增、时钟偏差超过 2 秒或积压持续增长，立即停止放量并进入回滚。

## 8. 回滚

1. 按相反顺序关闭 `JOB_HARD_DELETE_ENABLED`、`JOB_EXPIRY_CLEANUP_ENABLED`、`JOB_CANDIDATE_CLEANUP_ENABLED`、`JOB_REPLACEMENT_ENABLED`，并确认所有实例读取到同一配置。
2. 停止 API、Worker 和 scheduler，保留 MySQL 新增列、durable cleanup task、媒体生命周期记录、Redis AOF 和 revocation fence，优先 forward-fix。
3. 不得直接启动 003/004 之前的 Worker。需要回滚应用制品时，只能使用明确兼容 003/004 schema 和 session fencing 的版本，并继续禁止新旧版本混跑。
4. `phase10_down_001_job_lifecycle.sql` 仅允许在从未创建或回填任何新模型数据、001 归档的 backup rows/checksum 与当前备份表完全匹配，并取得破坏性回滚审批时执行。先使用迁移阶段的同一 `PHASE10_MYSQL` 再次核验目标，再用与 001 相同的算法重算并独立归档当前备份表的行数和 checksum。`phase10-down-backup-evidence.txt` 的两个值必须与 `phase10-001-output.txt` 精确一致，人工复核并记录审批后才能执行 down。Down 输出必须归档，命令必须为零退出，且 `phase10_restore_checksum_valid` 列的值为 `1`；任一保护条件拒绝后不得使用 `--force` 或其他方式绕过。

   ```bash
   set -euo pipefail
   test -r "$PHASE10_MYSQL_DEFAULTS_FILE"
   PHASE10_MYSQL=(mysql --defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE" --database="$DB_NAME" --show-warnings)
   "${PHASE10_MYSQL[@]}" --batch --skip-column-names -e "SELECT DATABASE(), @@hostname, @@port"
   "${PHASE10_MYSQL[@]}" --batch -e "SELECT COUNT(*) AS backup_rows, BIT_XOR(CRC32(CONCAT_WS('|', job_id, audit_status, COALESCE(expires_at, ''), COALESCE(deleted_at, ''), COALESCE(delist_reason, ''), version))) AS backup_checksum FROM phase10_job_lifecycle_backup" | tee phase10-down-backup-evidence.txt
   ```

   在这里停止。将新证据与 `phase10-001-output.txt` 精确比较并归档审批记录；任何差异都取消破坏性回滚。审批完成后才单独执行：

   ```bash
   set -euo pipefail
   test -r "$PHASE10_MYSQL_DEFAULTS_FILE"
   PHASE10_MYSQL=(mysql --defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE" --database="$DB_NAME" --show-warnings)
   "${PHASE10_MYSQL[@]}" --batch --skip-column-names -e "SELECT DATABASE(), @@hostname, @@port"
   "${PHASE10_MYSQL[@]}" < sql/migrations/phase10_down_001_job_lifecycle.sql | tee phase10-down-001-output.txt
   ```
5. 已经启用 replacement、产生 cleanup task 或执行媒体回填后，不做破坏性 schema 回滚；关闭开关、保留 durable 状态并发布修复版本。
6. 回滚后重新执行健康检查、`python -m scripts.phase10_clock_check` 和 `python -m scripts.phase10_preflight`，记录所有非零项和后续处置。

回滚不得删除 WIP/发布证据、迁移备份表、AOF 或 cleanup task，也不得用清库方式消除 preflight blocker。
