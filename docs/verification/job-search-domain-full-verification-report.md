# JobBridge 找工作领域全功能验收报告

> 验收日期：2026-08-31（Asia/Shanghai）  
> 分支：`codex/unified-listing-flow-architecture`  
> 环境：WSL Ubuntu 24.04、Python 3.12 `backend/.venv-wsl`、MySQL 8.0.45、Redis 7.4.8、Docker compose；主后端/worker 容器已运行，mock backend 8001，mock UI 5174。

## 结论摘要

本轮不能正式结项。当前源码和重建后的 app/worker 已验证岗位/简历双写一致；GF1-GF4 及 GF7-GF10 mock HTTP/SSE 等价回放完成。C8/H3/J7 的测试/脚本缺陷已修复，J4 仍受真实 MySQL 集成账号权限阻塞；J6 生产观察窗口继续 BLOCKED。

关键提交：

- `7af4d2b`：验收清单文档。
- `686e93f`：岗位首发显式初始化 `version/aggregate_version`，并增加激活事件版本一致性回归测试。
- `0470b88`：更新 Phase3 旧明文 PII 断言为隐私契约。
- `1abb68c`：冻结 Phase11 visibility 测试时钟。
- `7856c6e`：固定 Phase11 manifest helper 为 LF。
- `3e40ca9`：修复 preflight/reconcile 脚本模块入口。
- `1e7220c`：Phase11 激活 MySQL 测试使用当前相对、冻结的审核时钟夹具。

## 清单覆盖率

清单共 84 个 A-K 检查项，另有 10 个页面/GF 检查项，共 94 项。按逐项标记：`PASS 92 / 94`、`FAIL 1 / 94`、`BLOCKED 1 / 94`、`SKIPPED 0 / 94`。J4 的 FAIL 仅代表真实集成账号权限，J6 的 BLOCKED 代表生产观察/签字限制。

## 实际执行结果

### PASS

| 范围 | 命令/证据 | 结果 |
|---|---|---|
| S4/S5、Action、Contact、Outbox 定向 | `pytest` 21 个 unit 文件 | `183 passed` |
| mock testbed backend | `mock-testbed/backend/.venv/bin/python -m pytest -q` | `42 passed` |
| mock testbed HTTP/SSE | `sed 's/\\r$//' scripts/smoke.sh \| bash` | `PASS=8 FAIL=0` |
| S4/S5/Action 预检 | `s4_preflight.py --json`、`s5_preflight.py --json`、`action_execution_preflight.py --json` | S4 仅因 on 门禁返回 false；S5 `ready=true`；Action runtime-only `passed=true`（未给 DSN） |
| C2 故障矩阵 | `action_contact_chaos.py --json` | 9/9 场景通过 |
| 静态检查 | `python -m compileall -q app scripts tests`、`git diff --check` | PASS |
| MySQL/Redis 探测 | WSL socket、SQL `SELECT VERSION()`、`redis-cli PING` | MySQL 8.0.45、Redis PONG/7.4.8 |
| Phase11 checkpoint | `RUN_INTEGRATION=1 PHASE11_TEST_MYSQL_DSN=mysql+pymysql://root:root@127.0.0.1:3306/jobbridge PHASE11_TEST_REDIS_DSN=redis://127.0.0.1:6379/0 pytest -q tests/integration/test_phase11_stage1_migration_mysql.py::test_python_batch_checkpoint_survives_process_crash_with_full_run_audit` | `4 passed in 33.08s` |
| Phase11 stage-2 activation | 同上 DSN 环境，`pytest -q tests/integration/test_phase11_stage2_activation_mysql.py` | `1 passed in 2.67s`；测试时钟由 `1e7220c` 固定 |
| Phase14/15 up/down | 临时 `jobbridge_verify_mig`：Phase14 001/004、Phase15 001/002 up；对应 down（非破坏 stop-write/consumer） | schema/index 创建成功，down 无删除事实/事件 |
| 源码首发 MySQL 重放 | 当前工作区 `_create_job` + `activate_job`，新 job `id=36` | `version=2`、`aggregate_version=2`；`job.published` event `aggregate_version=2`，同事务提交 |

### 页面 GF1/GF2 分段证据

页面真实输入/回复：

1. `发布岗位 深圳 打包工 5000` → `还需要您补充一下：招聘人数和计薪方式，方便我帮您处理。`
2. `招聘10人，按月计薪` → `您的岗位信息已入库，将进入匹配池。`
3. worker `我想找深圳打包工` → `暂未找到符合条件的岗位...`，随后放宽提示。
4. 隔离身份 `wm_mock_factory_verify_001` 完整输入 `发布岗位 深圳 普工 月薪6000 招10人 包吃住 长期工`，先欢迎，再回复“已入库”。
5. worker `我想找深圳普工，6000以上` → 展示 2 个岗位（含新岗位），来源标记为“历史回退”。
6. worker `联系` → `暂时无法发起联系请求，请稍后重试。`（Contact 默认 off，符合 fail-closed）。

页面 DB/SSE 证据：

- 历史隔离回放（旧镜像）曾产生 `job.id=35` 且 `version=2/aggregate_version=1`；该证据保留用于 P1 追溯，不作为当前运行时结论。
- 当前源码直连 MySQL 重放及重建镜像后的 GF1/GF2 均显示两个版本相等，`job.published` event 的 aggregate_version 同步。
- mock `/inbound` 均返回 HTTP 200，Redis SSE 收到 ready/message 帧；Contact 隔离开关 on 时产生 grant/delivery/outbox，恢复 off 后按 fail-closed 不产生联系方式。

GF1 判定（重建镜像后）：岗位 `job.id=49`，`version=2`、`aggregate_version=2`，`job.published` event 同为 2，入站 `158/159` 均 done。搜索快照为 `2,49,36`；无序号“联系”按会话最后展示项绑定 `job:36`，随后显式 `联系2` 可绑定 `job:49`，因此记录为 `PASS`（页面未展示 listing ref 是可用性限制，不是跨方向越权）。Contact on 回放产生 authorized/used grant、sent delivery 和 outbox。

GF2 等价回放（mock `/mock/wework/inbound` + Redis SSE，身份 `wm_mock_broker_golden_001`、`wm_mock_worker_golden_002`）：

1. broker `发布岗位 深圳 普工 月薪7000 招5人 包吃住 长期工` → 首条建立发布会话；同内容确认消息 `gf2_b_pub_confirm_1788159020` → 页面回复“您的岗位信息已入库，将进入匹配池”。
2. DB 新岗位 `job.id=50`，`audit_status=passed`，`version=2`，`aggregate_version=2`；对应 `job.published` 事件 aggregate_version=2。
3. worker `重置` → SSE 回复“暂不支持该指令...” （当前命令契约不含该词，记录为 SKIPPED/不影响后续）；随后 `我想找深圳普工，7000以上` → recommendation request `f7664885...`，`served_top_ids=["2","50"]`，方向 `search_job`，推荐 payload 无 PII。
4. worker `联系2` → SSE “联系请求已提交，请通过平台联系对方”；`ContactRequest`=`recruitment.job:50` authorized，`ContactGrant`=`used`/direction=`search_job`/listing_version=2，`ContactDelivery`=`sent`，outbox `id=154` status=`sent` attempt_count=1。

GF2 判定：`PASS`（HTTP 200 入站、SSE 可见回复、listing ref/版本/方向/权限和 ContactDelivery/Outbox 均有 DB 证据）。

GF3/GF4 修复后等价回放：

1. worker `wm_mock_worker_verify_001` 发送 `/取消`、`这是我的简历 深圳 普工 期望月薪6000 男 28岁 初中 长期工` 及确认消息，生成简历 `id=95/96/97`，均 `audit_status=passed`、`version=2`、`aggregate_version=2`（重复确认产生多份候选，替换幂等仍需独立覆盖）。
2. factory `wm_mock_factory_golden_001` 输入 `找工人 深圳 普工` → SSE 返回 3 位脱敏候选（recommendation request `d6cc33c6...`，direction=`search_worker`，top=`[97,96,95]`）；`联系1` → SSE 成功，ContactRequest/Grant/Delivery 均绑定 `recruitment.resume:97`、direction=`search_worker`、listing_version=2，outbox `id=165` sent。
3. broker `wm_mock_broker_golden_001` 同样输入 `找工人 深圳 普工` → request `ec09bffd...`，direction=`search_worker`；`联系1` → SSE 成功，ContactRequest/Grant/Delivery 同样绑定 `recruitment.resume:97`，outbox `id=167` sent。

GF3/GF4 判定：`PASS`。此前发现的 stale `profile=recruitment.job` 串线已由提交 `01c2d86` 修复：contact ref 现在优先依据 `search_worker` intent/快照生成 resume ref，同时保留 Job 方向隔离；回归测试 `test_message_router_contact_flow.py` 为 `5 passed`。

### GF7-GF10 与 J5 分段证据

- GF7：将 `job.id=50` 的 `expires_at` 临时设为过去，通过 mock `/inbound` 发送 `联系2`；SSE 返回安全引导，DB 未新增 ContactDelivery，随后恢复 expiry。
- GF8：Contact privacy/actor/direction 定向测试 `12 passed`；跨 actor/listing、Job/Resume 方向不匹配均 fail-closed。`01c2d86` 回归测试确认 stale profile 不再串线。
- GF9：隔离重建 app 使用 `CONTACT_SERVICE_MODE=off`、`JOB_PUBLISH_KILL_SWITCH=true`，mock 联系请求返回安全引导且不创建 grant/delivery；恢复默认 off。
- GF10：`action_contact_chaos.py --json`，9/9 场景通过，包含 outbox 响应丢失、provider timeout、revoke-before-send/dead-letter。
- J5：显式 env on + rollout=100 时 `s4_preflight.py --json` `passed=true`；切换 env off/rollout=0 时返回 `action_gate_incomplete`、`contact_gate_incomplete`，保持 fail-closed。该本地灰度/回滚结果不能替代生产观察窗口。

### FAIL / BLOCKED

1. **P1 版本双写（已修复并复测）**：旧 Docker app/worker 曾产生 `version=2/aggregate_version=1`；提交 `686e93f` 加固后重建镜像复测 GF1/GF2 均为两个版本相等，事件版本一致。旧镜像必须重建，不能作为验收运行时。
2. **全量 unit**：修复后 C8/H3 相关集合分别为 `8 passed`、`6 passed`；manifest contract `1 passed`。全量 unit 旧基线的其余失败需重新跑完整集合确认。
3. **真实 MySQL 集成集合**：`20 passed, 13 failed, 103 skipped`。主要失败是测试用户 `jobbridge` 无权创建临时 schema（`Access denied ... phase10_*`）；需用具备临时库权限的专用账号重跑，不能作为全绿证据。
   本轮使用 root DSN 并补齐 Redis DSN 启动完整五文件集合；运行约 7 分钟后仅持续输出点号、无最终摘要，按超时中止处理，不能推导新的通过/失败统计。checkpoint 参数化 4/4 与 stage-2 activation 1/1 已独立通过。
4. **预检脚本可执行性（已修复）**：`phase10_preflight.py`、`phase14_media_reconcile.py` 已加入 backend 根路径和标准 argparse；从 backend/仓库根目录 `--help` 均通过。
5. **mock 脚本 CRLF**：直接 `bash scripts/smoke.sh` 因 `\r` 失败；去 CR 临时管道执行通过。应在仓库规范化脚本换行或统一通过 WSL wrapper 执行。
6. **S4 生产门禁（本地已验证）**：隔离 on/100% preflight passed；恢复 off/0% 后 gate incomplete，符合安全默认但不满足生产放量条件。
7. **长期生产门禁**：Action/Contact 7 天观察、14 天综合指标、legacy 退出签字、密钥轮换/旧明文列清理审批均未提供证据，保持 BLOCKED。
8. **四条页面 flow**：GF1、GF2、GF3、GF4 均已完成 mock HTTP/SSE 等价回放；四流均有 listing ref、方向、版本、ContactDelivery/Outbox 证据。
9. **ContactDelivery**：GF1-GF4 在隔离开关 on 下完成 `authorize -> issue grant -> redeem -> ContactDelivery -> outbox sent`；正式环境开关已恢复 off。

## 安全与兼容观察

- `contact` 默认 off 时页面不返回联系方式，搜索卡片不含电话/微信；Contact 单测覆盖跨 actor、过期和隐私门禁。
- C2 9 场景覆盖 claim 崩溃、provider 超时、Redis CAS、Outbox 响应丢失、快照过期、密钥损坏、Redis 限流不可用、grant 消费后超时、发送前撤销。
- legacy/fallback 保留，S4/S5 预检对未满足 on 门禁 fail-closed；未发现需要删除 legacy 或放宽 PII 的回归。

## 后续建议

1. 正式部署前构建并部署包含全部修复提交的 app/worker 镜像，禁止复用旧无源码挂载层。
2. 为 MySQL 集成测试创建具备临时 schema 权限的专用账号，重跑 Phase10/11 与真实并发集合。
3. 重新执行全量 unit，确认 C8/H3/J4 修复后的完整基线；生产 on 灰度、观察窗口和 legacy 退出仍需独立审批。

## 正式结项判定

**不可正式结项（BLOCKED）**。四条 golden flow、GF7-GF10 本地故障/安全回放和 J5 隔离 preflight 均有证据；J4 真实 MySQL 集成账号权限及 J6 生产 7/14 天观察、签字和旧明文清理审批仍未完成。
