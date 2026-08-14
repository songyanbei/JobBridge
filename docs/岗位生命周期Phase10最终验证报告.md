# 岗位生命周期 Phase 10 最终验证报告

## 1. 验证结论

- 分支：`codex/job-expiry-full-update-v05`
- 修复基线：`060f5fb`
- 最终代码验证 HEAD：`6198fe7`
- 验证日期：2026-08-11 至 2026-08-14（Asia/Shanghai）
- 结果：原 23 个问题及真实页面联调新发现的 3 个阻断问题均使用独立提交完成；V-01 至 V-05 与补充验收全部通过，未发现未关闭的 P1/P2/P3。
- 边界：未 rebase、未合并 `main`、未推送、未创建 PR。

## 2. 问题与提交追踪

| 序号 | 问题 | 提交 | 最终回归证据 |
| --- | --- | --- | --- |
| 1 | O-01 TTL preflight 范围 | `2c5219b` | V-01、V-05 |
| 2 | O-02 取消原因长度 | `9f80b7e` | V-01、V-02 |
| 3 | O-03 A→B→C 投影 | `9932713` | V-01、V-02 |
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
| 23 | N-18 发布手册 | `29af6d7` | V-01、V-02、V-04、V-05 |
| 24 | B-01 合法学历表述导致严格 MySQL 写入失败 | `3a67218` | 补充单测、严格 MySQL、真实对话 |
| 25 | B-02 全量更新显式字段丢失 | `8efed06` | 补充单测、严格 MySQL、真实对话与推荐 |
| 26 | B-03 岗位替换缺少推荐正文密钥发布门禁 | `6198fe7` | 补充单测、真实 MySQL/Redis preflight |

每项提交前均完成定向测试、受影响模块测试、真实服务测试（适用时）、`py_compile`、`git diff --check` 和独立只读评审。评审发现的问题均在对应问题内修复并重新执行全部门禁后才提交；没有使用空提交表示测试完成。

## 3. 验证环境

- WSL：`Ubuntu-24.04`
- Python：独立 venv `/tmp/jobbridge-phase10-venv`
- MySQL：`mysql:8.0`，隔离容器 `jobbridge-phase10-mysql-test`，宿主端口 `33306`
- Redis：`redis:7-alpine`，隔离容器 `jobbridge-phase10-redis-test`，宿主端口 `36379`
- Redis 策略：`maxmemory-policy=noeviction`、`appendonly=yes`、`appendfsync=always`
- 最终干净验收库：`jobbridge_phase10_verify`；与日常测试库 `jobbridge` 分离

以下命令均从 `backend` 目录执行。连接环境变量指向上述隔离服务，密码不写入报告。

## 4. V-01 全量单元测试

先按文件名排序固定全部 107 个 `tests/unit/test_*.py`，在工作区无代码变化的情况下使用实际 0-based 边界 `0..21`、`22..43`、`44..64`、`65..85`、`86..106` 分成五片，逐片显式传给 pytest：

```powershell
$all = Get-ChildItem tests/unit -Filter 'test_*.py' | Sort-Object Name
$bounds = @(@(0, 21), @(22, 43), @(44, 64), @(65, 85), @(86, 106))
foreach ($bound in $bounds) {
    $start, $end = $bound
    $slice = $all[$start..$end] | ForEach-Object { "tests/unit/$($_.Name)" }
    $joined = $slice -join ' '
    wsl -d Ubuntu-24.04 -- bash -lc "source /tmp/jobbridge-phase10-venv/bin/activate && python -m pytest $joined -q"
}
```

下表“固定序号”为便于人工阅读的 1-based 序号，与上述代码中的 0-based 边界一一对应。

| 分片 | 固定序号 | 首个文件 | 末个文件 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 1–22 | `test_admin_auth_default_password.py` | `test_dialogue_v2_criteria_patch_isolation.py` | 302 passed |
| 2 | 23–44 | `test_dialogue_v2_primary_rollback.py` | `test_multi_turn_upload_stage_b.py` | 453 passed |
| 3 | 45–65 | `test_multi_turn_upload_stage_c1.py` | `test_recommendation_exposure_service.py` | 382 passed |
| 4 | 66–86 | `test_recommendation_match_vectors.py` | `test_slot_schema.py` | 446 passed |
| 5 | 87–107 | `test_storage.py` | `test_worker_delivery_state_machine.py` | 328 passed |

V-01 合计：`1911 passed`，零失败。测试输出包含既有 `datetime.utcnow()`、`passlib crypt` 弃用警告，不影响本次通过结论。

## 5. V-02 真实 MySQL 与并发回归

主批命令：

```bash
RUN_INTEGRATION=1 python -m pytest \
  tests/integration/test_job_replace_mysql.py \
  tests/integration/test_media_target_cleanup_lock_order_mysql.py \
  tests/integration/test_outbox_claim_lock_scope_mysql.py \
  tests/integration/test_phase10_preflight_mysql.py \
  tests/integration/test_privacy_lock_order_mysql.py \
  tests/integration/test_privacy_redaction_batch_mysql.py \
  tests/integration/test_recommendation_candidate_delete_scrub.py \
  tests/integration/test_recommendation_plaintext_redaction.py \
  tests/integration/test_recommendation_privacy_delete.py \
  tests/integration/test_recommendation_session_outbox_consistency.py \
  tests/integration/test_session_commit_deadline_mysql.py \
  tests/integration/test_session_commit_lease_owner_mysql.py \
  tests/integration/test_target_cleanup_backfill_mysql.py \
  tests/integration/test_target_cleanup_checkpoint_redis_mysql.py \
  tests/integration/test_target_cleanup_lease_mysql.py \
  tests/integration/test_target_cleanup_upsert_mysql.py \
  tests/integration/test_ttl_outbox_delivery_lock_order_mysql.py -q
```

结果：`28 passed`。覆盖双连接 `REPEATABLE READ`、行锁/current read、唯一键竞争、lease owner fencing、500→499 批次竞态、跨批次锁序、outbox claim 范围、TTL terminalizer 竞争和 media/target 全局锁序。

Redis 真实停机用例单独执行：

```bash
REDIS_OUTAGE_CONTAINER=jobbridge-phase10-redis-test \
RUN_INTEGRATION=1 python -m pytest \
  tests/integration/test_session_commit_redis_unavailable_mysql.py -q
```

结果：`1 passed`；测试在 `finally` 中恢复 Redis，随后额外验证 `PONG`。V-02 合计：`29 passed`。

## 6. V-03 Redis 集成测试

```bash
RUN_INTEGRATION=1 python -m pytest \
  tests/integration/test_redis.py \
  tests/integration/test_recommendation_immutable_strategy_cache.py \
  tests/integration/test_recommendation_shadow_capacity.py \
  tests/integration/test_recommendation_shadow_mode.py \
  tests/integration/test_recommendation_shadow_multiworker_limits.py -q
```

结果：`56 passed`。覆盖 session/index Lua、首写前 wrong-type 预检、锁续租与 owner fencing、绝对 deadline、非法 operation、revocation fence、并发保存/清理、策略缓存和多 Worker 原子容量门禁。

结合 V-02 的 `test_target_cleanup_checkpoint_redis_mysql.py` 与真实停机恢复用例，Redis 故障、checkpoint 重试和恢复路径均通过。恢复后策略读取仍为 `noeviction`、AOF 开启、`appendfsync=always`。

## 7. V-04 时钟偏差

```bash
python -m scripts.phase10_clock_check
```

结果：`sampling_window_seconds=0.028506`，保守 `clock_skew_seconds=0.028080`，限制 `2.0`，`ready=true`。算法使用 MySQL 时间与 Redis 前后两个 `TIME` 端点的最大绝对差；采样变慢只会阻断，不会把超过 2 秒的偏差误判为通过。

## 8. V-05 干净数据库发布门禁

1. 在隔离 MySQL 容器内重建且仅重建 `jobbridge_phase10_verify`。
2. 加载当前完整 `sql/schema.sql`，创建空库 backup coverage 基线，写入合法 `ttl.job.days=30` 和 `ttl.job.candidate.days=7`。
3. 依次执行 003、004，再重复执行 003、004。四个命令均为零退出。
4. 核验列和索引：
   - `session_apply_lease_owner varchar(64) NULL`
   - `session_commit_deadline_epoch decimal(20,6) NULL`
   - `idx_session_commit_due(status,session_next_attempt_at,session_apply_locked_at,id)`，`NON_UNIQUE=1`
5. 按 `dry-run → apply → dry-run` 运行媒体回填，所有 blocker 为 0。
6. 按 `dry-run → apply → dry-run` 运行 Target cleanup 回填，最终 `missing=0`。
7. 运行时钟检查：保守偏差上界 `0.023929s`，`ready=true`。
8. 运行完整 preflight：schema、TTL、backup coverage、岗位状态、replacement graph、cleanup coverage、媒体 coverage、Redis policy 和 AUTO_INCREMENT blocker 全部为 0，最终 `ready=true`。

建库与基线命令使用权限为 `0600` 的 MySQL client defaults file；报告不记录密码。`PHASE10_MYSQL_ADMIN` 指向隔离容器中的 MySQL 8 管理端点：

```bash
set -euo pipefail
export PHASE10_VERIFY_DB=jobbridge_phase10_verify
export PHASE10_MYSQL_DEFAULTS_FILE=/run/secrets/jobbridge-phase10-verify.cnf
test -r "$PHASE10_MYSQL_DEFAULTS_FILE"
PHASE10_MYSQL_ADMIN=(mysql --defaults-extra-file="$PHASE10_MYSQL_DEFAULTS_FILE" --show-warnings)

"${PHASE10_MYSQL_ADMIN[@]}" -e "
  DROP DATABASE IF EXISTS jobbridge_phase10_verify;
  CREATE DATABASE jobbridge_phase10_verify
    CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
  GRANT ALL PRIVILEGES ON jobbridge_phase10_verify.* TO 'jobbridge'@'%';
"
"${PHASE10_MYSQL_ADMIN[@]}" "$PHASE10_VERIFY_DB" < sql/schema.sql
"${PHASE10_MYSQL_ADMIN[@]}" "$PHASE10_VERIFY_DB" -e "
  CREATE TABLE phase10_job_lifecycle_backup AS
    SELECT id AS job_id, audit_status, expires_at, deleted_at, delist_reason, version
    FROM job;
  ALTER TABLE phase10_job_lifecycle_backup ADD PRIMARY KEY (job_id);
  INSERT INTO system_config (config_key, config_value, value_type, description)
  VALUES
    ('ttl.job.days', '30', 'int', 'Phase10 verification'),
    ('ttl.job.candidate.days', '7', 'int', 'Phase10 verification');
"
```

003/004 幂等命令在同一个明确的 verify DB 上按顺序执行两轮：

```bash
set -euo pipefail
for migration in \
  phase10_003_session_commit_deadline.sql \
  phase10_004_session_commit_lease_owner.sql \
  phase10_003_session_commit_deadline.sql \
  phase10_004_session_commit_lease_owner.sql
do
  "${PHASE10_MYSQL_ADMIN[@]}" "$PHASE10_VERIFY_DB" < "sql/migrations/$migration"
done
```

运行 Python 门禁前显式切换应用连接。`DB_PASSWORD` 由测试环境安全注入，未写入命令或报告：

```bash
export DB_HOST=127.0.0.1 DB_PORT=33306 DB_NAME="$PHASE10_VERIFY_DB" DB_USER=jobbridge
test -n "${DB_PASSWORD:?DB_PASSWORD must be injected}"
export REDIS_HOST=127.0.0.1 REDIS_PORT=36379 APP_ENV=test

python -m scripts.backfill_media_lifecycle --output-dir /tmp/jobbridge-phase10-v05-media-dry
python -m scripts.backfill_media_lifecycle --apply --output-dir /tmp/jobbridge-phase10-v05-media-apply
python -m scripts.backfill_media_lifecycle --output-dir /tmp/jobbridge-phase10-v05-media-verify
python -m scripts.backfill_target_cleanup_tasks
python -m scripts.backfill_target_cleanup_tasks --apply
python -m scripts.backfill_target_cleanup_tasks
python -m scripts.phase10_clock_check
python -m scripts.phase10_preflight
```

## 8.1 真实联调阻断项补充验收

2026-08-14 使用最终代码 HEAD `6198fe7` 重建 WSL 审核镜像，API、消息 Worker、管理端和模拟企业微信入口均正常启动，`/health` 返回数据库连接正常。

1. B-01 学历规范化：真实 LLM 将“高中及以上”抽取后，持久化值规范为数据库枚举“高中”；此前“初中以上学历”场景也已验证规范为“初中”，严格 MySQL 未再发生截断或枚举写入错误。
2. B-02 全量更新：招聘者 `wm_mock_factory_002` 通过 `/更新岗位 124` 提交完整岗位文本。新岗位 `161` 自动审核通过并激活，旧岗位 `124` 变为 `delist_reason=replaced` 且软删除；新岗位的地址、接受夫妻工、学历、用工类型和合同类型分别为“昆山市开发区春旭路666号”、`true`、“高中”、“劳务派遣”、“短期合同”。
3. 推荐可见性：求职者 `wm_mock_worker_001` 搜索“找昆山服装厂，月薪7000以上”，最新 `recommendation_delivery.recommendation_context` 仅包含 `target_id=161`，不包含旧岗位 `124`，投递状态为 `sent`。
4. B-03 发布门禁：单密钥、密钥环、缺失密钥、活动版本不匹配均有自动化覆盖；评审额外发现版本 `65536` 无法由正文信封和 MySQL `UNSIGNED SMALLINT` 表示，已在配置层与 preflight 层同时阻断，`65535` 边界通过。

最终提交上的补充自动化结果：

- `python -m pytest -q tests/unit`：`1932 passed`，零失败。
- `tests/integration/test_job_replace_mysql.py` 与 `tests/integration/test_phase10_preflight_mysql.py`：真实 MySQL 8、Redis 7 环境下 `9 passed`，零失败。
- B-02 提交前相关模块回归：`241 passed`；独立严格 MySQL 持久化测试：`1 passed`。
- B-03 提交前相关模块回归：`70 passed`；独立真实 MySQL/Redis preflight：`3 passed`。
- 所有三项均通过 `py_compile`、`git diff --check` 和独立只读复审；评审提出的普通创建测试缺口及密钥版本上限 P1 均在对应提交前修复并重新执行门禁。

## 9. 最终静态与仓库状态证据

报告评审前实际执行：

```bash
python -m compileall -q app scripts tests
git diff --check
git status --short --branch
git rev-parse HEAD
git stash list
```

结果：

- `compileall`：退出码 0。
- `git diff --check`：退出码 0；补充验收前工作区干净，修订后验证报告是唯一已修改文件。
- 分支：`codex/job-expiry-full-update-v05`。
- 补充验收代码 HEAD：`6198fe7`。
- 状态：仅 `docs/岗位生命周期Phase10最终验证报告.md` 已修改，无其他修改。
- stash：`stash@{0}: wip/post-060f5fb-hardening-audit` 仍存在。
- 报告通过独立评审后，将使用 `git diff --cached --check` 检查已暂存报告；提交后再验证工作区干净和 stash 保留。

## 10. 残余风险

- 验收运行在单机 WSL 隔离容器中，不等价于生产多节点网络、存储延迟和故障域；生产发布仍必须按发布手册执行观察窗口和开关渐进放量。
- V-05 是空库的 schema/迁移幂等与发布门禁验证；历史数据形态、批量回填和并发竞态由 V-01/V-02 的构造数据覆盖，但没有用生产数据规模做容量压测。
- 单元测试仍报告既有 `datetime.utcnow()` 和 `passlib crypt` 弃用警告；升级 Python 3.13 前需要单独清理，不属于本次岗位生命周期改动。
- 本次没有实际部署、开启生产功能开关或执行破坏性 down migration；这些操作必须经过发布审批并遵守 `岗位生命周期Phase10发布手册.md`。
- `stash@{0}: wip/post-060f5fb-hardening-audit` 按约定保留，禁止整体恢复到当前分支；是否删除等待用户确认。

## 11. 最终状态要求

- 本报告独立只读评审无 P1/P2/P3 后，提交为 `docs(rollout): record phase10 verification evidence`。
- 提交后工作区必须干净，26 个问题提交和最终证据提交均可独立回溯。
- WIP stash 保持存在；不推送、不创建 PR，等待用户下一步指令。
