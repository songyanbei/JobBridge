# Mock 企业微信测试台 测试 Checklist

> 基于：`collaboration/features/phase7-mock-wework-testbed.md`
> 配套实施文档：`collaboration/features/phase7-mock-wework-testbed-test-implementation.md`
> 面向角色：测试 + 后端开发协同
> 状态：`draft`
> 创建日期：2026-04-19

## A. 测试前确认

- [ ] 已阅读 `collaboration/features/phase7-mock-wework-testbed.md`（主文档）
- [ ] 已阅读 `collaboration/features/phase7-mock-wework-testbed-test-implementation.md`
- [ ] 已阅读 `collaboration/features/phase7-mock-wework-testbed-dev-implementation.md`（了解切面位置）
- [ ] 已理解"只允许两处切口"（`api/mock_wework.py` + `wecom/client.py` 两段 flag 分支）
- [ ] 已理解"字段黑话锁死"清单（主文档 §3.1）
- [ ] 测试环境独立（MySQL / Redis 不污染 Phase 7 联调库）
- [ ] seed 数据已灌入（≥ 4 个 `wm_mock_*` 用户，覆盖 3 种 role）
- [ ] Redis 干净或 `FLUSHDB`
- [ ] Worker 进程已启动（`docker compose logs worker` 无错）
- [ ] 浏览器支持 EventSource（Chrome / Edge / Firefox 现代版本）

## B. Flag 与启动守卫

- [ ] TC-MW-1.1：`ENV=production + MOCK_WEWORK=true` 启动失败，错误含 `forbidden in production`
- [ ] TC-MW-1.2：`ENV=production + MOCK_WEWORK=false` 启动正常
- [ ] TC-MW-1.3：`ENV=development + MOCK_WEWORK=true` 启动正常，日志含 `MOCK_WEWORK enabled`
- [ ] TC-MW-1.4：`MOCK_WEWORK=false` 下 `GET /mock/wework/users` 返回 404
- [ ] TC-MW-1.5：`MOCK_WEWORK=false` 下 `WeComClient.send_text` 走真实路径（mock HTTP 断言命中）
- [ ] 启动日志中 `logger.warning` 行清晰可见
- [ ] 真实企微 webhook 路径（`POST /webhook/wecom` 验签）未被污染

## C. 身份入口

- [ ] TC-MW-2.1：`GET /mock/wework/users` 顶层字段 `== {errcode, errmsg, users}`
- [ ] `users[]` 每项字段 `== {external_userid, name, role, avatar}`
- [ ] TC-MW-2.2：所有返回 `external_userid` 均以 `wm_mock_` 开头（真实用户未暴露）
- [ ] TC-MW-2.3：`GET /oauth2/authorize?appid=X&redirect_uri=https://a.b/cb&state=Z` 返回 302，Location 含 `code=MOCK_CODE_xxx` 和 `state=Z`
- [ ] TC-MW-2.4：`GET /code2userinfo?access_token=MOCK&code=xxx` 响应 `{errcode:0, errmsg:"ok", external_userid, openid}`
- [ ] TC-MW-2.5：`code2userinfo` 响应所有 key 符合 `[a-z_]+`（无驼峰大写）

## D. 入站桥接

- [ ] TC-MW-3.1：完整合法 payload → HTTP 200；DB `wecom_inbound_event` +1；Redis `queue:incoming` +1
- [ ] TC-MW-3.2：缺 `MsgId` → `{errcode:40001, errmsg:"missing fields: ['MsgId']"}`
- [ ] TC-MW-3.3：同 `MsgId` 10 秒内重发 → 第二次返回 `(duplicate dropped)`；DB 和 Redis 各 1 条
- [ ] TC-MW-3.4：同 `MsgId` 跨 Redis TTL 再发 → `(duplicate in db)`
- [ ] TC-MW-3.5：mock 构造的 `WeComMessage` 字段结构与 `webhook.py` 解密后 100% 一致（snapshot）
- [ ] TC-MW-3.6：`queue:incoming` 的 JSON payload 字段集合与 `webhook.py` 入队 payload 100% 一致
- [ ] 真实 `webhook.py` 单测仍然通过（未被本任务污染）

## E. 出站拦截

- [ ] TC-MW-4.1：`MOCK_WEWORK=true + send_text(u,c)` → `publish` 1 次，`_http_post` 0 次
- [ ] TC-MW-4.2：拦截 payload 顶层字段 `== {touser, msgtype, agentid, text}`，`text.content` 存在
- [ ] TC-MW-4.3：`send_text_to_group(chat_id, c)` → channel = `mock:outbound:chat:{chat_id}`
- [ ] TC-MW-4.4：`git diff backend/app/wecom/client.py` 仅见 2 段 `if settings.mock_wework:` 新增，原函数体**未修改**
- [ ] TC-MW-4.5：`MOCK_WEWORK=false` 反转 → `_http_post` 1 次，`publish` 0 次

## F. SSE

- [ ] TC-MW-5.1：建立 `GET /mock/wework/sse?external_userid=X` + publish → 3s 内收到 `event: message`
- [ ] TC-MW-5.2：空闲 30s → 至少 2 个 `event: ping`
- [ ] TC-MW-5.3：客户端关闭 → `PUBSUB NUMSUB mock:outbound:X` = 0
- [ ] TC-MW-5.4：A/B 并发 SSE，给 A publish → 只 A 收到
- [ ] TC-MW-5.5：入站 → Worker → 出站 → SSE bot 回复 ≤ 10s
- [ ] nginx 未缓冲 SSE（前端气泡实时显示，不累积）
- [ ] `X-Accel-Buffering: no` 在响应 header 中存在

## G. 字段契约（强约束）

- [ ] `pytest backend/tests/unit/test_mock_wework_contract.py -v` 全绿
- [ ] 所有 snapshot 断言用 `==` 而非 `>=` / `issubset`
- [ ] `touser` / `toparty` / `totag` 值为 `str`（不是 list）
- [ ] 任何 mock 接口的响应或请求中**不含** `"userid"` 字段（`grep` 0 命中）
- [ ] 所有顶层响应都有 `errcode` 和 `errmsg`
- [ ] `msgtype` 枚举值从官方集合（`text / image / voice / video / file / textcard / news / markdown / miniprogram_notice / template_card`）中选取
- [ ] `card_type` / `template_card` 本次未实现，但 mock 前端渲染能兼容未来扩展（肉眼验证 payload 字段能透传）

## H. 前端双视角 UX

### H.1 B 模式（双栏）
- [ ] 访问 `/mock` → 红色横幅出现
- [ ] Banner 文字含 `MOCK-WEWORK TESTBED` 和「非真实企业微信环境」
- [ ] 尝试 URL 加 `?hideBanner=1` 等参数 → Banner 仍显示
- [ ] 双栏左右布局正确（窗口宽度 ≥ 1280px 时并排）
- [ ] 左栏切换器只显示 `factory / broker`
- [ ] 右栏切换器只显示 `worker`
- [ ] 切换身份时旧 SSE 连接关闭（DevTools Network 观察）
- [ ] 自己发送的消息气泡 `direction=in` 样式
- [ ] bot 回复气泡 `direction=out` 样式

### H.2 C 模式（多窗口）
- [ ] `/mock/single?external_userid=wm_mock_worker_001&role=worker` 进单视角
- [ ] `/mock/single?external_userid=wm_mock_factory_001&role=factory` 进单视角
- [ ] 两个窗口独立对话，互不干扰
- [ ] 刷新页面 URL 参数保留，身份不丢
- [ ] A 窗口发消息 B 窗口不出现（除非业务路由主动发给 B）

### H.3 身份切换器
- [ ] 下拉按 role 分组（求职者 / 招聘者（厂家）/ 招聘者（中介））
- [ ] 支持 `filterable` 搜索（输入 `worker` 过滤）
- [ ] 只显示 `wm_mock_*` 前缀用户
- [ ] label 含名字 + external_userid

## I. SSE 稳定性（长时观察）

- [ ] 建立 SSE 保持 30 分钟空闲 → EventSource.readyState 始终 OPEN (1)
- [ ] 观察到至少 120 个 `event: ping`
- [ ] 模拟网络抖动（DevTools → offline 10s → online）→ 自动重连
- [ ] 发送 10 条消息 → 全部收到 bot 回复，无丢失
- [ ] 切换身份 10 次 → 连接数不叠加（DevTools Network 确认）
- [ ] 30 分钟内无 `SSE error` 日志（忽略 ping 阶段短暂抖动）

## J. 迁移演练（业务代码 0 改动证明）

- [ ] 切分支 `mock-migration-dry-run`
- [ ] 按主文档 §8 的 6 步删除 mock 层
- [ ] `git diff main..HEAD -- backend/app/api/webhook.py backend/app/api/events.py backend/app/api/admin/ backend/app/services/` 输出**空**（不含新增 `mock_outbound_bus.py`）
- [ ] `git diff main..HEAD -- backend/app/models.py` 输出**空**
- [ ] `git diff main..HEAD -- backend/app/wecom/client.py` 输出**空**（两段 flag 分支已删）
- [ ] 剩余 diff 只涉及：`config.py`（删字段）、`main.py`（删 include_router）、新增文件删除、`.env.example` 删行、`seed_mock_users.sql` 保留
- [ ] `pytest backend/tests/` 全绿
- [ ] 演练分支最终不 merge，截图留档

## K. 安全验收

- [ ] 生产 `.env` 中 `MOCK_WEWORK=false`（部署前必查）
- [ ] CI 配置禁止 `main` / `release/*` 分支出现 `MOCK_WEWORK=true`（grep 卡点）
- [ ] 模拟 UI 无 JWT 颁发路径（抓包确认无 `Set-Cookie: jobbridge_admin_token`）
- [ ] `GET /admin/me` 不可被 mock 用户身份访问（返回 401）
- [ ] 红色横幅不可通过前端任何方式隐藏（试过 URL 参数、localStorage、CSS 禁用均失败）
- [ ] 业务统计 SQL 加 `AND external_userid NOT LIKE 'wm_mock_%'` 后，数字与真实环境一致
- [ ] `/mock/wework/sse` 未鉴权，但仅 `MOCK_WEWORK=true` 才存在（双重隔离）

## L. 回归

- [ ] `pytest backend/tests/unit/` 全绿
- [ ] `RUN_INTEGRATION=1 pytest backend/tests/integration/` 全绿
- [ ] `pytest backend/tests/unit/test_mock_wework*.py -v` 单独跑也全绿
- [ ] `cd frontend && npm run build` 成功
- [ ] `docker compose up -d` 5 容器 healthy
- [ ] `/admin/login` 正常，管理员后台业务未受影响
- [ ] `POST /webhook/wecom` 真实企微路径验签单测依然通过
- [ ] Phase 7 既有回归（417 passed）无新增失败

## M. 性能参考（说明性验收）

- [ ] 确认测试报告明确标注"SSE 延迟不能作为真企微 P95 验收证据"
- [ ] 仅用于采集"入队到 Worker 处理完"的内部基线
- [ ] 记录 LLM 单次调用延迟（与企微无关）

## N. 测试报告交付

- [ ] `collaboration/handoffs/phase7-mock-wework-testbed-test-report.md` 已产出
- [ ] 报告包含：
  - [ ] 环境信息（OS / 分支 commit / Docker / Python / Node 版本）
  - [ ] 全部 TC-MW-* 用例执行结果表
  - [ ] 字段契约响应体完整 JSON（至少 5 个路由各 1 份）
  - [ ] 双视角 UX 截图（B + C 模式各 ≥ 1 张）
  - [ ] SSE 30 分钟稳定性观察结论 + 日志片段
  - [ ] 迁移演练 `git diff` 统计截图
  - [ ] pytest-cov 覆盖率报告（关键模块达标）
  - [ ] P2 / P3 缺陷清单 + 处理建议
- [ ] P0 / P1 缺陷全部清零

## O. 最终签收

- [ ] 本 Checklist 每一项勾选
- [ ] 开发 Checklist 每一项勾选
- [ ] 测试报告已评审
- [ ] 主文档 §7 验收标准全部通过
- [ ] 测试台可正式交付，纳入长期维护
