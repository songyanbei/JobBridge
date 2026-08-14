# 岗位生命周期 Phase 10 最终验证报告

## 1. 验证结论

- 分支：`codex/job-expiry-full-update-v05`
- 修复基线：`060f5fb`
- 最终代码验证 HEAD：`bc7e7cf`
- 验证日期：2026-08-11 至 2026-08-15（Asia/Shanghai）
- 结果：39 个已知问题均使用独立提交完成修复；最终代码 HEAD `bc7e7cf` 的 V-01 至 V-05 与旧 schema 兼容冒烟全部通过，最终自动化验证未发现未关闭的 P1/P2/P3。页面联调是 `6198fe7` 上的历史补充证据，不声明已在最终 HEAD 重跑。
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

每项功能提交前均执行定向测试、受影响模块测试、适用的真实服务测试、Python 编译检查、`git diff --check` 和独立只读评审。评审提出的可执行问题均在对应问题内修复并重新通过门禁后提交；没有使用空提交表示测试完成。

## 3. 验证环境

- WSL：`Ubuntu-24.04`
- Python：独立 venv `/tmp/jobbridge-phase10-venv`
- MySQL：`mysql:8.0`，容器 `jobbridge-phase10-mysql-test`，宿主端口 `33306`
- Redis 策略：`maxmemory-policy=noeviction`、`appendonly=yes`、`appendfsync=always`
- 最终队列测试 Redis：容器 `jobbridge-phase10-final-redis`，宿主端口 `36380`
- 原演示服务使用 `36379` 且有 Worker 消费队列，因此最终队列集成测试使用 `36380` 隔离运行，避免测试消息被演示 Worker 抢占。
- 最终干净验收数据库：`jobbridge_phase10_final_verify`，与日常开发数据隔离。

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

固定排序全部 107 个 `tests/unit/test_*.py`，按文件边界分为五片运行：

| 分片 | 文件序号 | 结果 |
| --- | --- | --- |
| 1 | 1–22 | 303 passed |
| 2 | 23–44 | 483 passed |
| 3 | 45–65 | 401 passed |
| 4 | 66–86 | 448 passed |
| 5 | 87–107 | 344 passed |

V-01 合计：`1979 passed`，零失败。测试仅产生既有 `datetime.utcnow()` 和 `passlib crypt` 弃用警告。

实际五分片生成和执行命令（从 `backend` 目录运行）：

```powershell
$all = Get-ChildItem tests/unit -Filter 'test_*.py' | Sort-Object Name
$bounds = @(@(0, 21), @(22, 43), @(44, 64), @(65, 85), @(86, 106))
foreach ($bound in $bounds) {
    $start, $end = $bound
    $files = $all[$start..$end] | ForEach-Object { "tests/unit/$($_.Name)" }
    $joined = $files -join ' '
    wsl -d Ubuntu-24.04 -- bash -lc "cd /mnt/d/work/JobBridge/_worktrees/job-expiry-full-update-v05/backend && /tmp/jobbridge-phase10-venv/bin/python -m pytest -q $joined"
}
```

## 5. V-02/V-03 真实 MySQL 与 Redis

- Phase 10 CI 主集成清单在 MySQL 8 与隔离 Redis 7 上运行：`95 passed`。
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

首次最终采样：

```json
{"sampling_window_seconds": 0.0284, "clock_skew_seconds": 0.027921, "max_clock_skew_seconds": 2.0, "ready": true}
```

V-05 完整门禁前再次采样：`clock_skew_seconds=0.027955`，`ready=true`。两次均远低于 2 秒限制。

两次均使用相同目标环境运行：

```bash
DB_HOST=127.0.0.1 DB_PORT=33306 \
REDIS_HOST=127.0.0.1 REDIS_PORT=36380 \
python -m scripts.phase10_clock_check
```

## 7. V-05 干净数据库发布门禁

在独立数据库 `jobbridge_phase10_final_verify` 完成：

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
export PHASE10_VERIFY_DB=jobbridge_phase10_final_verify
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
    ('ttl.job.candidate.days', '7', 'int', 'Phase10 verification');
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
export DB_NAME=jobbridge_phase10_final_verify DB_USER=root
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

以下页面联调执行于 2026-08-14、代码 HEAD `6198fe7`，用于确认 B-01 至 B-03 的端到端体验；它不是最终 HEAD `bc7e7cf` 的页面重跑证据：

- 学历规范化：LLM 产生“高中及以上”“初中以上学历”等表达时，严格 MySQL 持久化为合法枚举值，不再截断或写入失败。
- 全量更新：招聘者通过 `/更新岗位 124` 提交完整岗位文本，新岗位 `161` 自动审核激活，旧岗位 `124` 以 `delist_reason=replaced` 软删除；地址、夫妻工、学历、用工类型和合同类型均完整保留。
- 推荐可见性：求职者搜索后推荐上下文只包含新岗位 `161`，不包含旧岗位 `124`，投递状态为 `sent`。
- 正文密钥：单密钥、密钥环、缺失密钥、活动版本不匹配和版本上限均有自动化门禁。

`6198fe7` 之后的 R-01 至 R-12 和 T-01 共 13 个提交，分别由对应提交的定向测试、真实 MySQL/Redis 门禁，以及最终 HEAD 上的 V-01 `1979 passed`、主集成 `95 passed`、停机 `1 passed`、补充 Redis `5 passed` 和 Stage A `1 passed` 覆盖。由于本轮未在 `bc7e7cf` 重跑完整页面流程，本报告不将历史页面联调作为最终 HEAD 的合并门禁依据。

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
- 提交后工作区干净，39 个问题提交与验证报告均可独立回溯。
- 不推送、不创建 PR、不合并 `main`，等待用户下一步指令。
