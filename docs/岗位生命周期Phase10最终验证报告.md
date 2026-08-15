# 岗位生命周期 Phase 10 最终验证报告

## 1. 验证结论

- 分支：`codex/job-expiry-full-update-v05`
- 修复基线：`060f5fb`
- 最终代码验证 HEAD：`1c81739`
- 验证日期：2026-08-11 至 2026-08-15（Asia/Shanghai）
- 结果：55 个已知问题均使用独立提交完成修复；`d13ec5e` 的 V-01 至 V-05 全量证据继续有效，之后五项收口修复在最终代码 HEAD `1c81739` 上通过完整单元测试及对应的真实 MySQL/Redis 集成门禁，独立评审未发现未关闭的 P1/P2/P3。Stage A 旧 schema 兼容冒烟是前次最终验收证据；页面联调是 `6198fe7` 上的历史补充证据，不声明已在最终 HEAD 重跑。
- 边界：未 rebase、未合并 `main`、未推送、未创建 PR；保留的 WIP stash 未恢复或删除。

## 2. 问题与提交追踪

| 序号 | 问题 | 提交 | 最终证据 |
| --- | --- | --- | --- |
| 1 | O-01 TTL preflight 范围 | `2c5219b` | V-01、V-05 |
| 2 | O-02 取消原因长度 | `9f80b7e` | V-01、V-02 |
| 3 | O-03 A→B→C 双向投影 | `9932713` | V-01、V-02 |
| 4 | O-04 replaced 岗位恢复 | `5913f20` | V-01、V-02 |
| 5 | O-05 MySQL RR 幂等重查 | `c2bcb61` | V-01、V-02 |
| 6 | N-09 Redis durability policy | `ccd1592` | V-01、V-03、V-05 |
| 7 | N-05 Redis 索引类型预检 | `15c982e` | V-01、V-03 |
| 8 | N-04 会话反向索引与 revocation fence | `a4aecce` | V-01、V-03 |
| 9 | N-06 Durable session 绝对截止 | `ca3fc2f` | V-01、V-02、V-03、V-05 |
| 10 | N-07 Session claim owner fencing | `b78cbc0` | V-01、V-02、V-05 |
| 11 | N-14 非法 session operation | `d358290` | V-01、V-03 |
| 12 | N-08 Redis unavailable deadline | `77ac952` | V-01、V-02、V-03 |
| 13 | N-01 Target task 幂等创建 | `718d72c` | V-01、V-02 |
| 14 | N-02 Target cleanup lease fencing | `f936326` | V-01、V-02 |
| 15 | N-03 Redis 失效检查点 | `977cbf4` | V-01、V-02、V-03 |
| 16 | N-10 500 条隐私清理遗漏 | `7a56a7d` | V-01、V-02 |
| 17 | N-11 跨批次全局锁序 | `f7ffb03` | V-01、V-02 |
| 18 | N-12 Outbox claim 锁范围 | `ea19e43` | V-01、V-02 |
| 19 | N-13 TTL outbox→delivery 锁序 | `79016a6` | V-01、V-02 |
| 20 | N-15 Media/target cleanup 锁序 | `34b4d41` | V-01、V-02 |
| 21 | N-16 Session schema rollout gate | `0b284c6` | V-01、V-02、V-05 |
| 22 | N-17 Target cleanup 回填顺序 | `4db63ff` | V-01、V-02、V-05 |
| 23 | N-18 发布手册 | `29af6d7` | V-01、V-04、V-05 |
| 24 | B-01 学历字段导致严格 MySQL 写入失败 | `3a67218` | 单测、真实 MySQL、页面联调 |
| 25 | B-02 全量更新显式字段丢失 | `8efed06` | 单测、真实 MySQL、页面联调 |
| 26 | B-03 replacement 正文密钥发布门禁缺失 | `6198fe7` | 单测、真实 MySQL/Redis preflight |
| 27 | R-01 Undo TOCTOU 可覆盖激活后的岗位 | `3a3952f` | 定性交错单测、真实 MySQL |
| 28 | R-02 软删后媒体被提前删除 | `e16bdd3` | 生命周期单测、媒体清理集成测试 |
| 29 | R-03 down migration 覆盖迁移后合法变更 | `4f254b7` | 迁移漂移与 ABA 回归测试 |
| 30 | R-04 preflight 将上线后新增/硬删误判为 blocker | `6d55b38` | 真实 MySQL preflight |
| 31 | R-05 全部开关关闭仍创建首次发布候选 | `36ff25e` | 上传入口单测、真实 MySQL |
| 32 | R-06 缺少旧 schema 兼容版本发布阶段 | `435ed91` | Stage A 旧 schema 冒烟 |
| 33 | R-07 迁移门禁缺分类计数与 live/expected checksum | `8686f4d` | 迁移证据与真实 MySQL preflight |
| 34 | R-08 `/更新岗位` 空草稿 TTL 未从命令时开始 | `c1cb36a` | 命令/草稿 TTL 单测 |
| 35 | R-09 CI 缺 Redis 且遗漏新增集成测试 | `1ff76e4` | workflow 审查、CI 命令本地复跑 |
| 36 | R-10 pending replacement 创建缺结构化审计 | `b20f6fc` | 事务边界、幂等与日志失败单测 |
| 37 | R-11 replacement 图片未复用 pending_operation_id | `7aa3e85` | 普通/替换图片关联与失效会话单测 |
| 38 | R-12 生命周期配置缺失时静默回退 | `cea68d1` | 缺失、非法、恢复与限流单测 |
| 39 | T-01 合成 rollout 测试依赖共享城市字典和过期时间 | `bc7e7cf` | V-01 五分片全量回归 |
| 40 | F-01 历史候选迁移 TTL 使用本地时区导致晚 8 小时 | `2f07625` | V-01、V-02、V-05 |
| 41 | F-02 V2 clarification/conflict 绕过上传草稿 TTL | `1e30068` | V-01、定向路由回归 |
| 42 | F-03 Undo 在数据库提交前删除 Redis 快照 | `4350caa` | V-01、V-02、真实 MySQL/Redis 故障注入 |
| 43 | F-04 首次发布 pending/rejected 候选缺创建事件 | `b5c6a48` | V-01、V-02 |
| 44 | F-05 destructive down 后错误运行最终 preflight | `70a5240` | V-01、V-02、Stage A 冒烟 |
| 45 | F-06 合成 rollout 测试依赖共享岗位类别本体 | `24b08b8` | V-01、200 项 dialogue 回归 |
| 46 | F-07 迁移测试旧 schema 夹具缺 inbound 表 | `8d31a46` | V-02 真实 MySQL |
| 47 | G-01 destructive down 改写历史 Job `updated_at` | `0761921` | V-01、V-02、V-05 |
| 48 | G-02 down 专用校验器接受缺失 inbound 表 | `a3576c1` | V-01、V-02 |
| 49 | G-03 图片消息可续用过期首发草稿 | `b27562b` | V-01、V-02、V-03 |
| 50 | G-04 synthetic rollout 导入期依赖真实字典数据库 | `d13ec5e` | V-01、fresh-process 导入与 provider 刷新回归 |
| 51 | H-01 草稿过期后排队图片可误改旧岗位/简历 | `71529e5` | 完整单测、真实 MySQL/Redis、独立复审 |
| 52 | H-02 down 校验接受同名残缺 inbound 表 | `26d7089` | rollout 单测、destructive-down 真实 MySQL、独立复审 |
| 53 | H-03 down inbound 列合同非精确匹配 | `393398a` | rollout 单测、五类真实 MySQL 结构负例、独立复审 |
| 54 | H-04 down 未校验 inbound 表引擎与默认字符集 | `0c835ab` | MyISAM/latin1 真实 MySQL 负例、独立复审 |
| 55 | H-05 down inbound 索引合同单向匹配 | `1c81739` | 额外 UNIQUE/INVISIBLE 真实 MySQL 负例、独立复审 |

每项功能提交前均执行定向测试、受影响模块测试、适用的真实服务测试、Python 编译检查、`git diff --check` 和独立只读评审。评审提出的可执行问题均在对应问题内修复并重新通过门禁后提交；没有使用空提交表示测试完成。

## 3. 验证环境

- WSL：`Ubuntu-24.04`
- Python：独立 venv `/tmp/jobbridge-phase10-venv`
- MySQL：`mysql:8.0`，容器 `jobbridge-phase10-mysql-test`，宿主端口 `33306`
- Redis 策略：`maxmemory-policy=noeviction`、`appendonly=yes`、`appendfsync=always`
- 最终队列测试 Redis：容器 `jobbridge-phase10-final-redis`，宿主端口 `36380`
- 原演示服务使用 `36379` 且有 Worker 消费队列，因此最终队列集成测试使用 `36380` 隔离运行，避免测试消息被演示 Worker 抢占。
- 最终干净验收数据库：`jobbridge_phase10_final_verify_d13ec5e`，与日常开发数据隔离。

除 V-01 的 PowerShell 分片驱动和 Stage A 独立命令外，V-02 至 V-04 的 shell 命令均在同一个 WSL shell 中先执行以下上下文；密码由测试环境注入，不写入报告：

```bash
cd /mnt/d/work/JobBridge/_worktrees/job-expiry-full-update-v05/backend
source /tmp/jobbridge-phase10-venv/bin/activate
export APP_ENV=test RUN_INTEGRATION=1
export DB_HOST=127.0.0.1 DB_PORT=33306 DB_NAME=jobbridge DB_USER=root
test -n "${DB_PASSWORD:?DB_PASSWORD must be injected}"
export REDIS_HOST=127.0.0.1 REDIS_PORT=36380 REDIS_DB=0
export RECOMMENDATION_CONTENT_KEY=phase10-test-key
```

## 4. V-01 全量单元测试

固定排序全部 108 个 `tests/unit/test_*.py`，按文件边界分为五片运行：

| 分片 | 文件序号 | 结果 |
| --- | --- | --- |
| 1 | 1–22 | 304 passed |
| 2 | 23–44 | 488 passed |
| 3 | 45–66 | 416 passed |
| 4 | 67–88 | 461 passed |
| 5 | 89–108 | 328 passed |

V-01 合计：`1997 passed`，零失败。测试仅产生既有 `datetime.utcnow()` 和 `passlib crypt` 弃用警告。

实际五分片生成和执行命令（从 `backend` 目录运行）：

```powershell
$all = Get-ChildItem tests/unit -Filter 'test_*.py' | Sort-Object Name
$bounds = @(@(0, 21), @(22, 43), @(44, 65), @(66, 87), @(88, 107))
foreach ($bound in $bounds) {
    $start, $end = $bound
    $files = $all[$start..$end] | ForEach-Object { "tests/unit/$($_.Name)" }
    $joined = $files -join ' '
    wsl -d Ubuntu-24.04 -- bash -lc "cd /mnt/d/work/JobBridge/_worktrees/job-expiry-full-update-v05/backend && /tmp/jobbridge-phase10-venv/bin/python -m pytest -q $joined"
}
```

## 5. V-02/V-03 真实 MySQL 与 Redis

- Phase 10 CI 主集成清单在 MySQL 8 测试库与隔离 Redis 7 上运行：`103 passed`。新增用例覆盖过期图片草稿在下载和媒体写入前被拒绝，旧媒体与 Redis 草稿同步收敛。
- Redis 真实停机、fail-closed terminalization 与恢复：`1 passed`。
- 补充 Redis 推荐、多 Worker 与策略测试：`5 passed`。
- Redis 恢复后读取：`maxmemory-policy=noeviction`、`appendonly=yes`、`appendfsync=always`，健康检查 `PONG`。

覆盖 replacement RR 并发、outbox/delivery/cleanup 锁序、lease owner fencing、session deadline、wrong-type 原子预检、revocation fence、隐私批次、Redis 故障恢复和多 Worker 容量门禁。

主集成清单（环境变量指向 `33306` 和隔离 Redis `36380`）：

```bash
RUN_INTEGRATION=1 python -m pytest -p no:cacheprovider -q \
  tests/integration/test_search_sql_mysql.py \
  tests/integration/test_phase10_preflight_mysql.py \
  tests/integration/test_phase10_down_migration_mysql.py \
  tests/integration/test_job_candidate_creation_gate_mysql.py \
  tests/integration/test_job_media_hard_delete_delay_mysql.py \
  tests/integration/test_job_replace_mysql.py \
  tests/integration/test_media_target_cleanup_lock_order_mysql.py \
  tests/integration/test_outbox_claim_lock_scope_mysql.py \
  tests/integration/test_privacy_lock_order_mysql.py \
  tests/integration/test_privacy_redaction_batch_mysql.py \
  tests/integration/test_session_commit_deadline_mysql.py \
  tests/integration/test_session_commit_lease_owner_mysql.py \
  tests/integration/test_target_cleanup_backfill_mysql.py \
  tests/integration/test_target_cleanup_checkpoint_redis_mysql.py \
  tests/integration/test_target_cleanup_lease_mysql.py \
  tests/integration/test_target_cleanup_upsert_mysql.py \
  tests/integration/test_ttl_outbox_delivery_lock_order_mysql.py \
  tests/integration/test_redis.py
```

真实 Redis 停机测试（测试在 `finally` 恢复容器）：

```bash
REDIS_OUTAGE_CONTAINER=jobbridge-phase10-final-redis \
RUN_INTEGRATION=1 python -m pytest -p no:cacheprovider -q \
  tests/integration/test_session_commit_redis_unavailable_mysql.py
```

补充 Redis/多 Worker 测试：

```bash
RUN_INTEGRATION=1 python -m pytest -p no:cacheprovider -q \
  tests/integration/test_recommendation_immutable_strategy_cache.py \
  tests/integration/test_recommendation_shadow_capacity.py \
  tests/integration/test_recommendation_shadow_mode.py \
  tests/integration/test_recommendation_shadow_multiworker_limits.py
```

## 6. V-04 时钟偏差

`d13ec5e` 全量验收时采样：

```json
{"sampling_window_seconds": 0.024636, "clock_skew_seconds": 0.024198, "max_clock_skew_seconds": 2.0, "ready": true}
```

V-05 完整门禁前再次采样：`sampling_window_seconds=0.030694`、`clock_skew_seconds=0.03032`、`ready=true`。两次均远低于 2 秒限制。

两次均使用相同目标环境运行：

```bash
DB_HOST=127.0.0.1 DB_PORT=33306 \
REDIS_HOST=127.0.0.1 REDIS_PORT=36380 \
python -m scripts.phase10_clock_check
```

## 7. V-05 干净数据库发布门禁

在新建的独立数据库 `jobbridge_phase10_final_verify_d13ec5e` 完成：

1. 从不可变 Stage A 提交 `499eb929b75ad2f208d306b62157d8ded0119f33` 加载旧 schema。
2. 依次执行 001、002、003、004，再重复执行 003、004，全部成功。
3. 迁移证据中 backup/job/classification 行数均为 0，`live_checksum_valid=1`、`expected_checksum_valid=1`。
4. 核验 `session_commit_deadline_epoch decimal(20,6) NULL`、`session_apply_lease_owner varchar(64) NULL`，以及索引列序 `status,session_next_attempt_at,session_apply_locked_at,id`。
5. 媒体回填按 `dry-run→apply→dry-run` 执行，所有 blocker 为 0。
6. Target cleanup 回填按 `dry-run→apply→dry-run` 执行，最终 `missing=0`。
7. 完整 preflight 的 schema、TTL、backup integrity、岗位状态、replacement graph、cleanup/media coverage、Redis policy、正文密钥和 AUTO_INCREMENT blocker 全部为 0，最终 `ready=true`。

复现命令如下。数据库凭据通过权限为 `0600` 的 MySQL defaults file 和环境注入，不记录在报告中：

```bash
set -euo pipefail
cd /mnt/d/work/JobBridge/_worktrees/job-expiry-full-update-v05/backend
source /tmp/jobbridge-phase10-venv/bin/activate
export PHASE10_VERIFY_DB=jobbridge_phase10_final_verify_d13ec5e
export PHASE10_MYSQL_DEFAULTS_FILE=/run/secrets/jobbridge-phase10-verify.cnf
test -r "$PHASE10_MYSQL_DEFAULTS_FILE"
PHASE10_MYSQL=(mysql --defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE" \
  -h127.0.0.1 -P33306 --show-warnings)

"${PHASE10_MYSQL[@]}" -e "
  DROP DATABASE IF EXISTS ${PHASE10_VERIFY_DB};
  CREATE DATABASE ${PHASE10_VERIFY_DB}
    CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
"
"${PHASE10_MYSQL[@]}" "$PHASE10_VERIFY_DB" \
  < /mnt/d/work/JobBridge/_worktrees/phase10-stage-a-final/backend/sql/schema.sql
"${PHASE10_MYSQL[@]}" "$PHASE10_VERIFY_DB" -e "
  INSERT INTO system_config
    (config_key, config_value, value_type, description)
  VALUES
    ('ttl.job.days', '30', 'int', 'Phase10 verification'),
    ('ttl.job.candidate.days', '7', 'int', 'Phase10 verification'),
    ('ttl.hard_delete.delay_days', '7', 'int', 'Phase10 verification');
"

for migration in \
  phase10_001_job_lifecycle_additive.sql \
  phase10_002_media_dead_letter.sql \
  phase10_003_session_commit_deadline.sql \
  phase10_004_session_commit_lease_owner.sql \
  phase10_003_session_commit_deadline.sql \
  phase10_004_session_commit_lease_owner.sql
do
  "${PHASE10_MYSQL[@]}" "$PHASE10_VERIFY_DB" \
    < "sql/migrations/$migration"
done
```

迁移后在同一个 shell 和 `backend` 工作目录中，以同一数据库和隔离 Redis 运行回填与门禁：

```bash
export APP_ENV=test DB_HOST=127.0.0.1 DB_PORT=33306
export DB_NAME=jobbridge_phase10_final_verify_d13ec5e DB_USER=root
test -n "${DB_PASSWORD:?DB_PASSWORD must be injected}"
export REDIS_HOST=127.0.0.1 REDIS_PORT=36380
export RECOMMENDATION_CONTENT_KEY=phase10-test-key

python -m scripts.backfill_media_lifecycle --output-dir /tmp/phase10-media-dry
python -m scripts.backfill_media_lifecycle --apply --output-dir /tmp/phase10-media-apply
python -m scripts.backfill_media_lifecycle --output-dir /tmp/phase10-media-verify
python -m scripts.backfill_target_cleanup_tasks
python -m scripts.backfill_target_cleanup_tasks --apply
python -m scripts.backfill_target_cleanup_tasks
python -m scripts.phase10_clock_check
python -m scripts.phase10_preflight
```

## 8. Stage A 旧 schema 兼容冒烟

使用 detached Stage A 提交 `499eb92` 的应用代码与旧 `schema.sql`，在真实 MySQL 中创建临时数据库并运行：

```bash
cd /mnt/d/work/JobBridge/_worktrees/phase10-stage-a-final/backend
RUN_INTEGRATION=1 RUN_PHASE10_STAGE_A=1 \
PHASE10_STAGE_A_ROOT=/mnt/d/work/JobBridge/_worktrees/phase10-stage-a-final \
PYTHONPATH=. /tmp/jobbridge-phase10-venv/bin/python -m pytest --rootdir=. \
  --confcutdir=/mnt/d/work/JobBridge/_worktrees/job-expiry-full-update-v05/backend/tests/rollout \
  --import-mode=importlib -q \
  /mnt/d/work/JobBridge/_worktrees/job-expiry-full-update-v05/backend/tests/rollout/test_phase10_stage_a_old_schema.py
```

结果：`1 passed`。覆盖旧 schema 下岗位列表及分页、详情、CSV、审核队列和审核详情，并确认 Phase 10 新列尚不存在；临时数据库在测试结束后自动删除。

## 9. 页面联调历史补充证据

以下页面联调执行于 2026-08-14、代码 HEAD `6198fe7`，用于确认 B-01 至 B-03 的端到端体验；它不是最终 HEAD `1c81739` 的页面重跑证据：

- 学历规范化：LLM 产生“高中及以上”“初中以上学历”等表达时，严格 MySQL 持久化为合法枚举值，不再截断或写入失败。
- 全量更新：招聘者通过 `/更新岗位 124` 提交完整岗位文本，新岗位 `161` 自动审核激活，旧岗位 `124` 以 `delist_reason=replaced` 软删除；地址、夫妻工、学历、用工类型和合同类型均完整保留。
- 推荐可见性：求职者搜索后推荐上下文只包含新岗位 `161`，不包含旧岗位 `124`，投递状态为 `sent`。
- 正文密钥：单密钥、密钥环、缺失密钥、活动版本不匹配和版本上限均有自动化门禁。

`6198fe7` 之后的 R-01 至 R-12、T-01、F-01 至 F-07 和 G-01 至 G-04 共 24 个追踪问题，分别由对应提交的定向测试、真实 MySQL/Redis 门禁，以及 `d13ec5e` 上的 V-01 `1997 passed`、主集成 `103 passed`、停机 `1 passed` 和补充 Redis `5 passed` 覆盖。Stage A `1 passed` 为前次验收证据；由于本轮未在 `1c81739` 重跑完整页面流程，本报告不将历史页面联调作为最终 HEAD 的合并门禁依据。

H-01 与 H-02 完成后，在最终代码 HEAD `26d7089` 上重新执行增量收口门禁：完整 `tests/unit` 为 `2003 passed`；完整 `tests/integration/test_redis.py` 在真实 Redis/MySQL 下为 `56 passed`；完整 `tests/integration/test_phase10_down_migration_mysql.py` 在 MySQL 8 隔离数据库下为 `4 passed`。H-01 进一步覆盖草稿过期后的两张排队图片、pending/rejected candidate、已挂载媒体重放、失效或被驳回的精确 Job/Resume 目标，以及媒体删除状态；H-02 进一步覆盖 inbound 同名 `id`-only 表、字段类型/默认值漂移、缺失索引和前缀唯一索引。两项均在修复评审意见后重跑全部本项门禁，并分别获得“无可执行问题，可提交”的独立只读评审结论。

H-03 至 H-05 完成后，在最终代码 HEAD `1c81739` 再次重跑相同收口门禁，结果仍为完整单元 `2003 passed`、完整 Redis/MySQL 集成 `56 passed`、完整 destructive-down MySQL `4 passed`。新增真实 MySQL 负例覆盖额外无默认必填列、ENUM 字面值大小写、latin1 字符列、`created_at ON UPDATE`、生成列、MyISAM、latin1 默认表属性、额外 `UNIQUE(from_userid)` 和 required index `INVISIBLE`；同时确认额外普通非唯一索引按发布策略允许。三项分别完成实现、门禁、独立只读评审和独立提交。

## 10. 最终静态检查与仓库状态

报告评审和提交前后执行：

```bash
python -m compileall -q app scripts tests
git diff --check
git status --short --branch
git rev-parse HEAD
git stash list
```

要求与结果：编译和 diff 检查退出码为 0；报告提交后工作区必须干净；`stash@{0}: wip/post-060f5fb-hardening-audit` 必须继续保留。

## 11. 残余风险

- 验收在单机 WSL 隔离容器完成，不等价于生产多节点网络、存储延迟和故障域；生产发布仍须按发布手册执行观察窗口和开关渐进放量。
- V-05 使用空旧 schema 验证迁移幂等和发布门禁；历史数据形态及并发竞态由 V-01/V-02 构造数据覆盖，但未使用生产数据规模做容量压测。
- Python 3.13 前仍需处理既有 `passlib crypt` 弃用项；该事项不属于本次岗位生命周期变更。
- 未实际执行生产部署、开启生产开关或运行破坏性 down migration；这些操作须经发布审批。
- WIP stash 按约定保留，禁止整体恢复到当前分支；是否删除等待用户确认。

## 12. 完成条件

- 本报告经独立只读评审无 P1/P2/P3 后独立提交。
- 提交后工作区干净，55 个问题提交与验证报告均可独立回溯。
- 不推送、不创建 PR、不合并 `main`，等待用户下一步指令。
