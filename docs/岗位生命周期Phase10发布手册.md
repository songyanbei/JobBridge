# 岗位生命周期 Phase 10 发布手册

本手册从 `backend` 目录执行命令。数据库连接、Redis 连接和对象存储配置必须指向同一套待发布环境；所有功能开关在发布开始前保持 `false`。

## 1. 旧 schema 兼容版本验证

Phase 10 必须先独立部署旧 schema 兼容制品，再进入停服迁移。该制品固定为 commit `499eb929b75ad2f208d306b62157d8ded0119f33`（`feat(job): support nullable expiry reads`），只包含 nullable expiry 读取兼容，不读取任何 Phase 10 新表或新列。禁止用最终 schema-aware 制品替代本阶段制品，也禁止在本阶段冒烟通过前执行任何 Phase 10 迁移。

1. 记录旧 schema 兼容制品、最终制品和回滚制品的 commit 及镜像 digest。验证兼容 commit 是最终 commit 的祖先，并从干净的 detached worktree 构建兼容制品：

   ```bash
   set -euo pipefail
   export PHASE10_STAGE_A_COMMIT=499eb929b75ad2f208d306b62157d8ded0119f33
   export PHASE10_FINAL_COMMIT=<本次最终待发布完整commit>
   export PHASE10_RELEASE_ROOT=<最终发布源码绝对路径>
   export PHASE10_STAGE_A_ROOT=<独立临时worktree绝对路径>
   test "$(git -C "$PHASE10_RELEASE_ROOT" rev-parse HEAD)" = "$PHASE10_FINAL_COMMIT"
   test -z "$(git -C "$PHASE10_RELEASE_ROOT" status --porcelain)"
   git -C "$PHASE10_RELEASE_ROOT" merge-base --is-ancestor "$PHASE10_STAGE_A_COMMIT" "$PHASE10_FINAL_COMMIT"
   git -C "$PHASE10_RELEASE_ROOT" worktree add --detach "$PHASE10_STAGE_A_ROOT" "$PHASE10_STAGE_A_COMMIT"
   test "$(git -C "$PHASE10_STAGE_A_ROOT" rev-parse HEAD)" = "$PHASE10_STAGE_A_COMMIT"
   test -z "$(git -C "$PHASE10_STAGE_A_ROOT" status --porcelain)"
   ```

2. 在旧 schema 的隔离预发布 MySQL 上运行自动化冒烟。Python 环境必须从 Stage A 的 `backend/requirements.txt` 创建；以下隔离参数防止最终源码的 `conftest.py` 或 `app` 污染 Stage A 导入。测试会验证实际导入路径、旧 schema 不含 Phase 10 新列，并覆盖岗位列表分页、详情、CSV、审核队列和审核详情：

   ```bash
   cd "$PHASE10_STAGE_A_ROOT/backend"
   RUN_INTEGRATION=1 RUN_PHASE10_STAGE_A=1 \
   PHASE10_STAGE_A_ROOT="$PHASE10_STAGE_A_ROOT" PYTHONPATH=. \
   python -m pytest --rootdir=. \
     --confcutdir="$PHASE10_RELEASE_ROOT/backend/tests/rollout" \
     --import-mode=importlib -q \
     "$PHASE10_RELEASE_ROOT/backend/tests/rollout/test_phase10_stage_a_old_schema.py"
   ```

3. 数据库仍保持旧 schema，将全部 API、消息 Worker、scheduler 和 session recovery Worker 部署为 Stage A 同一镜像 digest，确认旧实例全部退出。此时不得部署或启动最终制品。
4. 对真实发布环境执行带鉴权的只读冒烟并归档请求、响应状态、制品 commit 和镜像 digest：后台岗位列表第 1/2 页、岗位详情、岗位 CSV 导出、审核工作台队列和审核详情。任一接口 5xx、字段解析失败、分页重复/遗漏或制品版本不一致，立即回滚 Stage A 并停止发布。
5. 只有自动化与真实环境冒烟全部通过、证据完成归档后，才允许进入发布冻结。Stage A worktree 和测试数据库按变更记录清理，不得把测试数据库当作生产迁移目标。

## 2. 发布冻结

1. 确认 `JOB_REPLACEMENT_ENABLED=false`、`JOB_EXPIRY_CLEANUP_ENABLED=false`、`JOB_CANDIDATE_CLEANUP_ENABLED=false`、`JOB_HARD_DELETE_ENABLED=false`。在所有 API、消息 Worker、scheduler 和 session recovery Worker 上预先配置同一份 `RECOMMENDATION_CONTENT_KEY` 或 `RECOMMENDATION_CONTENT_KEY_RING`，并统一 `RECOMMENDATION_CONTENT_KEY_ACTIVE_VERSION`；密钥材料只能来自 secrets/KMS，不得写入日志或发布证据。
2. 停止全部 Stage A API、消息 Worker、scheduler 和 session recovery Worker，等待当前请求结束并确认进程全部退出。
3. 禁止新旧 API 或 Worker 混跑，禁止跨 003/004 schema 边界滚动发布。
4. 完成并校验 MySQL 全量备份。确认 Redis AOF 文件及持久化目录可恢复；不得清空 revocation fence。

旧 Worker 不写 session deadline 或 lease owner，会绕过绝对截止和 owner fencing。发现任何旧实例仍存活时，发布必须停止。

## 3. 迁移

为 MySQL 8 客户端准备权限为 `0600` 的配置文件，至少在 `[client]` 中配置待发布环境的 `host`、`port`、`user`、`password` 和 TLS 参数。迁移账号除常规 DDL/DML 权限外，必须具备 `CREATE ROUTINE`、`ALTER ROUTINE`、`EXECUTE` 和 `TRIGGER` 权限，用于创建、执行及移除破坏性回滚写入围栏。配置文件不得提交到代码库。所有迁移和回滚必须复用同一个配置文件和数据库名；先输出实际数据库、主机和端口，人工核对后再执行：

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
PHASE10_BACKUP_EVIDENCE_SQL="SELECT COUNT(*), COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', job_id, audit_status, COALESCE(expires_at, ''), COALESCE(deleted_at, ''), COALESCE(delist_reason, ''), version, source_updated_at))), 0), COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', job_id, expected_audit_status, COALESCE(expected_expires_at, ''), COALESCE(expected_deleted_at, ''), COALESCE(expected_delist_reason, ''), expected_version, expected_updated_at, COALESCE(expected_activated_at, ''), COALESCE(expected_candidate_expires_at, '')))), 0) FROM phase10_job_lifecycle_backup"
"${PHASE10_MYSQL[@]}" --batch --skip-column-names -e "$PHASE10_BACKUP_EVIDENCE_SQL" | tee phase10-001-backup-evidence.tsv
# 归档 backup rows/checksum/expected-live checksum；同时归档 001 最后一行的
# 三类 source/live 计数、live/expected checksum 和两个 valid 字段；
# 核对三类计数逐项相等、backup_rows=job_rows、live_checksum=expected_live_checksum，
# 且 live_checksum_valid=1、expected_checksum_valid=1。任一不符立即停止。
"${PHASE10_MYSQL[@]}" < sql/migrations/phase10_002_media_dead_letter.sql | tee phase10-002-output.txt
"${PHASE10_MYSQL[@]}" < sql/migrations/phase10_003_session_commit_deadline.sql | tee phase10-003-output.txt
"${PHASE10_MYSQL[@]}" < sql/migrations/phase10_004_session_commit_lease_owner.sql | tee phase10-004-output.txt
```

003、004 必须在所有旧 Worker 停止后执行。迁移后不得启动旧版本进程。

## 4. 媒体回填

先 dry-run 并归档 CSV/JSON 报告，再 apply，最后重新 dry-run：

```bash
python -m scripts.backfill_media_lifecycle --output-dir phase10-media-dry-run
python -m scripts.backfill_media_lifecycle --apply --output-dir phase10-media-apply
python -m scripts.backfill_media_lifecycle --output-dir phase10-media-verify
```

最终报告中的 missing、repair-required、media delete dead-letter、invalid JSON、unresolved reference 和 conflict 阻断项必须全部为 0。`non_deleted_soft_deleted_media_key_count` 是历史硬删待处理量，不是硬删开关开启前的 blocker；回填只补齐 ownership coverage，不得把激活岗位或简历的媒体置为 `delete_pending`。第一次 dry-run 发现缺口时返回非零是预期行为，不能因此跳过 apply 后的复核。

## 5. Target cleanup 回填

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

## 6. 最终版本部署

1. 迁移和全部回填门禁通过后，构建一次最终 schema-aware 制品；API、消息 Worker 和 scheduler 必须使用同一镜像 digest。最终制品只能在 Phase 10 schema 上启动，Stage A 不得在迁移后重新启动。
2. 保持四个岗位生命周期开关为 `false`，同时启动新版本 API、消息 Worker、scheduler 和 session recovery Worker；不得让旧版本回流。此时 `JOB_REPLACEMENT_ENABLED=false` 同时禁止 replacement 和首次发布流程持久化 pending/rejected 候选，auto-pass 首次发布仍可直接激活，不得出现 `expires_at IS NULL AND candidate_expires_at IS NOT NULL` 的新增 Job。
3. 检查 `/health`、消息 Worker/scheduler/session recovery Worker heartbeat、各进程版本和镜像 digest、Redis durability policy、推荐正文活动密钥版本及数据库连接；四类进程必须与已记录的同一制品和同一配置版本一致，只核对密钥版本和可解析状态，不输出密钥材料。
4. 执行一轮不创建 replacement 的基础消息、后台查询和推荐发送 smoke test。
5. 再次执行 `python -m scripts.phase10_clock_check` 和 `python -m scripts.phase10_preflight`，结果必须通过。

## 7. 开关顺序

每一步至少观察一个完整 worker 调度周期；任一门禁或监控异常时停止，不得继续打开后续开关。

1. 先以待启用的相同配置执行 `JOB_REPLACEMENT_ENABLED=true python -m scripts.phase10_preflight`，确认 `recommendation_content_key_unavailable=0` 且 `ready=true`；再在所有 API、消息 Worker、scheduler 和 session recovery Worker 同步开启 `JOB_REPLACEMENT_ENABLED=true`，验证首次发布与 replacement 两类候选的创建、审核、取消、激活和推荐消息投递。缺少活动版本密钥或各实例配置版本不一致时禁止开启。
2. 开启 `JOB_CANDIDATE_CLEANUP_ENABLED=true`，验证过期候选进入 media/target durable cleanup。
3. 开启 `JOB_EXPIRY_CLEANUP_ENABLED=true`，验证到期岗位软删除、version 增长和 cleanup task 创建。
4. 确认 coverage blocker 为 0、无 dead-letter 且 hard-delete 延迟窗口满足后，开启 `JOB_HARD_DELETE_ENABLED=true`。硬删任务负责把已超过延迟的历史媒体置为 `delete_pending`，并由逐行 media/target fail-closed 门禁阻止岗位提前物理删除；禁止由回填脚本代替该步骤。
5. 持续运行 hard-delete、media 和 target worker，直到 `non_deleted_soft_deleted_media_key_count=0`、media/target backlog 收敛且无 dead-letter，再次执行完整 preflight 并归档结果，才可结束发布窗口。

开关变更要求 API、Worker 和 scheduler 使用同一配置版本；禁止只重启部分实例造成行为混合。

## 8. 监控

发布窗口持续观察以下指标和结构化事件：

- `outbox_health`、`outbox_dead_letter`、`outbox_pending_age`。
- `session_commit_health`、`session_commit_pending_age`、session deadline terminal 数量。
- `media_cleanup_health`、`media_cleanup_dead_letter`、`delete_pending` 和 `dead_letter` 数量。
- `target_cleanup_task` 的 `pending`、`processing`、`retry_wait`、`dead_letter` 数量和最老任务年龄。
- `passed_without_activation`、`invalid_unactivated_candidate`、`active_replacement_graph_missing`、`soft_deleted_without_cleanup_task`。
- MySQL deadlock、lock wait timeout、Worker heartbeat、队列积压和 API 5xx。

每次开关变更后重新运行 `python -m scripts.phase10_preflight`。任一 blocker 非零、dead-letter 新增、时钟偏差超过 2 秒或积压持续增长，立即停止放量并进入回滚。

## 9. 回滚

1. 先在所有 API、消息 Worker、scheduler 和 session recovery Worker 同步关闭 `JOB_REPLACEMENT_ENABLED`，确认所有实例读取到同一配置；该开关是 replacement 和首次发布 pending/rejected 候选的生产门禁，必须先于任何候选消费者关闭。
2. 关闭 `JOB_HARD_DELETE_ENABLED` 和 `JOB_EXPIRY_CLEANUP_ENABLED`，但保持 `JOB_CANDIDATE_CLEANUP_ENABLED=true`，继续运行 candidate、media 和 target cleanup Worker。直到未激活候选 backlog、media/target cleanup backlog 均收敛且无 processing/retry_wait/dead-letter，连续一个完整调度周期没有新增候选后，才同步关闭 `JOB_CANDIDATE_CLEANUP_ENABLED`。
3. 停止 API、Worker 和 scheduler，保留 MySQL 新增列、durable cleanup task、媒体生命周期记录、Redis AOF 和 revocation fence，优先 forward-fix。
4. 不得直接启动 003/004 之前的 Worker。需要回滚应用制品时，只能使用明确兼容 003/004 schema 和 session fencing 的版本，并继续禁止新旧版本混跑。
5. `phase10_down_001_job_lifecycle.sql` 仅允许在从未创建或回填任何新模型数据、001 归档的 backup rows、原始 backup checksum 和 expected-live checksum 与当前备份表完全匹配，并且不存在 `session_pending`、非空 `session_commit_deadline_epoch` 或非空 `session_apply_lease_owner` 时执行；001 安装的 inbound INSERT/UPDATE/DELETE 数据库围栏会从 guard 开始持续阻止收尾 Worker 写入，直到脚本安全移除 003/004 两列后才撤销。任何 durable session 状态必须先由当前版本正常收敛，不得通过手工清空字段绕过。取得破坏性回滚审批后，先使用迁移阶段的同一 `PHASE10_MYSQL` 再次核验目标，再用与 001 相同的算法重算并独立归档三项证据。`phase10-down-backup-evidence.tsv` 必须与 `phase10-001-backup-evidence.tsv` 逐字节一致，人工复核并记录审批后才能执行 down。Down 输出必须归档，命令必须为零退出，且 `phase10_restore_checksum_valid` 列的值为 `1`；任一保护条件拒绝后不得使用 `--force` 或其他方式绕过。

   ```bash
   set -euo pipefail
   test -r "$PHASE10_MYSQL_DEFAULTS_FILE"
   PHASE10_MYSQL=(mysql --defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE" --database="$DB_NAME" --show-warnings)
   "${PHASE10_MYSQL[@]}" --batch --skip-column-names -e "SELECT DATABASE(), @@hostname, @@port"
   PHASE10_BACKUP_EVIDENCE_SQL="SELECT COUNT(*), COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', job_id, audit_status, COALESCE(expires_at, ''), COALESCE(deleted_at, ''), COALESCE(delist_reason, ''), version, source_updated_at))), 0), COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', job_id, expected_audit_status, COALESCE(expected_expires_at, ''), COALESCE(expected_deleted_at, ''), COALESCE(expected_delist_reason, ''), expected_version, expected_updated_at, COALESCE(expected_activated_at, ''), COALESCE(expected_candidate_expires_at, '')))), 0) FROM phase10_job_lifecycle_backup"
   "${PHASE10_MYSQL[@]}" --batch --skip-column-names -e "$PHASE10_BACKUP_EVIDENCE_SQL" | tee phase10-down-backup-evidence.tsv
   cmp --silent phase10-001-backup-evidence.tsv phase10-down-backup-evidence.tsv
   ```

   在这里停止。将新证据与 `phase10-001-backup-evidence.tsv` 精确比较并归档审批记录；任何差异都取消破坏性回滚。审批完成后才单独执行：

   ```bash
   set -euo pipefail
   test -r "$PHASE10_MYSQL_DEFAULTS_FILE"
   PHASE10_MYSQL=(mysql --defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE" --database="$DB_NAME" --show-warnings)
   "${PHASE10_MYSQL[@]}" --batch --skip-column-names -e "SELECT DATABASE(), @@hostname, @@port"
   test "$(wc -l < phase10-001-backup-evidence.tsv)" -eq 1
   read -r PHASE10_ARCHIVED_ROWS PHASE10_ARCHIVED_BACKUP_CHECKSUM PHASE10_ARCHIVED_EXPECTED_CHECKSUM < phase10-001-backup-evidence.tsv
   [[ "$PHASE10_ARCHIVED_ROWS" =~ ^[0-9]+$ && "$PHASE10_ARCHIVED_BACKUP_CHECKSUM" =~ ^[0-9]+$ && "$PHASE10_ARCHIVED_EXPECTED_CHECKSUM" =~ ^[0-9]+$ ]]
   {
     printf 'SET @phase10_archived_backup_rows=%s, @phase10_archived_backup_checksum=%s, @phase10_archived_expected_live_checksum=%s;\n' "$PHASE10_ARCHIVED_ROWS" "$PHASE10_ARCHIVED_BACKUP_CHECKSUM" "$PHASE10_ARCHIVED_EXPECTED_CHECKSUM"
     cat sql/migrations/phase10_down_001_job_lifecycle.sql
   } | "${PHASE10_MYSQL[@]}" | tee phase10-down-001-output.txt
   ```
6. 已经启用 replacement、产生 cleanup task 或执行媒体回填后，不做破坏性 schema 回滚；关闭开关、保留 durable 状态并发布修复版本。
7. 未执行 destructive schema down 时属于应用/开关回滚：保留 Phase 10 schema，重新执行健康检查、`python -m scripts.phase10_clock_check` 和 `python -m scripts.phase10_preflight`，记录所有非零项和后续处置。
8. 已经执行 destructive schema down 时，不得再启动最终 schema-aware 制品，也不得运行 `python -m scripts.phase10_preflight`；该 preflight 依赖已删除的新列和新表，报结构不存在不代表有效验收。使用仍指向同一生产目标的最终工具环境运行专用只读校验：

   ```bash
   python -m scripts.phase10_down_verify | tee phase10-down-verify.json
   test "$(python -c 'import json; print(str(json.load(open("phase10-down-verify.json"))["ready"]).lower())')" = true
   ```

   `old_schema_required_tables_missing`、`phase10_job_columns_remaining`、`phase10_session_columns_remaining`、`old_job_table_contract_mismatch`、`old_inbound_table_contract_mismatch`、`old_inbound_constraints_mismatch`、`old_inbound_triggers_remaining`、`old_inbound_column_contract_mismatch`、`old_inbound_index_contract_mismatch`、`old_job_column_contract_mismatch`、`phase10_tables_remaining`、`phase10_fences_remaining`、`backup_expected_columns_remaining` 和 `restored_job_backup_mismatch` 必须全部为 `0`，`ready=true`。`job` 必须保持 InnoDB。inbound 门禁会精确核对 Stage A 的 InnoDB 引擎与默认字符集/排序规则、完整字段语义、表约束、零触发器，以及主键、唯一键和 Worker/session 索引列序。未声明的 CHECK、FOREIGN KEY、PRIMARY 或 UNIQUE 约束及任意命名的 inbound trigger 都会阻断；所有声明索引必须可见。额外普通非唯一索引仅允许可直接映射完整物理列的 BTREE，prefix、expression、无物理列或其他索引类型都会阻断。同名空表或残缺表不得视为回滚成功。必需的旧 schema 表为 `job`、`phase10_job_lifecycle_backup` 和 `wecom_inbound_event`，缺少任一表都视为破坏性回滚失败。然后将数据库连接切回不可变 Stage A 制品 `499eb929b75ad2f208d306b62157d8ded0119f33`，确认最终制品全部退出，再执行带鉴权健康检查和旧 schema 只读冒烟：后台岗位列表第 1/2 页、岗位详情、岗位 CSV 导出、审核工作台队列和审核详情。全部通过后归档 down 输出、专用校验 JSON、Stage A commit/镜像 digest 和请求响应证据；任何 5xx、结构错误、数据不一致或进程版本混跑都视为回滚失败。

回滚不得删除 WIP/发布证据、迁移备份表、AOF 或 cleanup task，也不得用清库方式消除 preflight blocker。
