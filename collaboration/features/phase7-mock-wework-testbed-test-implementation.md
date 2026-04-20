# Mock 企业微信测试台 测试实施文档

> 基于：`collaboration/features/phase7-mock-wework-testbed.md`
> 配套 Checklist：`collaboration/features/phase7-mock-wework-testbed-test-checklist.md`
> 面向角色：测试 + 后端开发协同
> 状态：`draft`
> 创建日期：2026-04-19

## 1. 测试目标

验证 Mock 企业微信测试台做到三件事：

1. **功能可用**：双视角 UX（B 分栏 + C 多窗口）、SSE 流式回复、消息往返闭环
2. **字段契约正确**：所有接口的请求/响应/SSE 帧字段名与企业微信官方契约 **1:1 对齐**
3. **隔离可靠**：`MOCK_WEWORK=false` 或 `ENV=production` 下完全不可用；业务代码 diff 为 0

任一项失败，本任务视为未通过。

## 2. 测试分层与策略

| 层级 | 工具 | 覆盖范围 |
|---|---|---|
| 单元测试 | pytest + FastAPI TestClient | 后端路由、flag 守卫、出站拦截、字段契约 snapshot |
| 集成测试 | pytest + 真实 Redis + 真实 MySQL（RUN_INTEGRATION=1） | 入站 → Worker → 出站全链路、幂等跨进程、SSE 真实订阅 |
| 前端单测 | Vitest（可选） | 组件渲染、EventSource 管理、身份切换 |
| 手工 E2E | 浏览器 + 双栏页面 | 双视角交互、SSE 气泡、多窗口演示 |
| 迁移演练 | 脚本 + git diff | 按主文档 §8 步骤拆除 mock 层，验证业务代码 0 改动承诺 |

### 2.1 核心风险

1. **字段漂移**：未来某次误改把 `external_userid` 改成 `userid`，或把 `msgtype` 改成 `type`。用**严格 snapshot 断言**防御
2. **真实路径被污染**：`WeComClient._http_post` 被误改或 flag 判断逻辑翻转。用 mock + 断言调用次数防御
3. **幂等失效**：mock 入站的幂等逻辑与 `webhook.py` 不一致导致队列堆积。用共享 snapshot 测试防御
4. **SSE 资源泄漏**：客户端断开后后端 pubsub 不释放，Redis 连接爆涨。用 `PUBSUB NUMSUB` 查询断言
5. **生产误启**：运维误配 `MOCK_WEWORK=true` 上生产。用启动 assert + CI grep 防御

## 3. Fixtures 设计

新增或扩展 `backend/tests/conftest.py`：

```python
import pytest, time, json
from app.models import User

@pytest.fixture
def mock_wework_enabled(monkeypatch):
    """开启 MOCK_WEWORK 并重载 settings。"""
    monkeypatch.setenv("MOCK_WEWORK", "true")
    monkeypatch.setenv("ENV", "development")
    from app import config as cfg
    from importlib import reload
    reload(cfg)
    yield
    reload(cfg)

@pytest.fixture
def mock_users(db):
    """注入 4 个 wm_mock_* 用户。"""
    users = [
        User(external_userid="wm_mock_worker_001",  role="worker",  display_name="张工"),
        User(external_userid="wm_mock_worker_002",  role="worker",  display_name="李师傅"),
        User(external_userid="wm_mock_factory_001", role="factory", display_name="华东电子厂"),
        User(external_userid="wm_mock_broker_001",  role="broker",  display_name="速聘中介"),
    ]
    for u in users:
        db.merge(u)
    db.commit()
    yield users
    db.query(User).filter(User.external_userid.like("wm_mock_%")).delete(synchronize_session=False)
    db.commit()

@pytest.fixture
def sample_inbound_payload():
    """样板入站 payload，字段名照搬企微 XML 标签。"""
    return {
        "ToUserName":   "wwmock_corpid",
        "FromUserName": "wm_mock_worker_001",
        "CreateTime":   int(time.time()),
        "MsgType":      "text",
        "Content":      "我想找深圳打包工",
        "MsgId":        f"mock_msgid_{int(time.time())}_{hex(id(object()))[2:]}",
        "AgentID":      "1000002",
    }

@pytest.fixture
def sse_collect():
    """读取 StreamingResponse 并解析前 N 个 SSE 事件。"""
    def _collect(iter_source, max_events=5, timeout=3.0):
        events = []
        deadline = time.time() + timeout
        buf = ""
        for chunk in iter_source:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="ignore")
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                ev = {}
                for line in raw.strip().split("\n"):
                    if line.startswith("event: "):
                        ev["event"] = line[7:]
                    elif line.startswith("data: "):
                        ev["data"] = line[6:]
                if ev:
                    events.append(ev)
                if len(events) >= max_events:
                    return events
            if time.time() > deadline:
                break
        return events
    return _collect
```

## 4. 测试用例清单

### 4.1 Flag 隔离

| ID | 用例 | 预期 | 类型 |
|---|---|---|---|
| TC-MW-1.1 | `ENV=production + MOCK_WEWORK=true` 加载配置 | `RuntimeError`，应用不启动 | 单测 |
| TC-MW-1.2 | `ENV=production + MOCK_WEWORK=false` 加载配置 | 正常启动 | 单测 |
| TC-MW-1.3 | `ENV=development + MOCK_WEWORK=true` 启动 | 启动成功；日志出现 `MOCK_WEWORK enabled` | 单测 |
| TC-MW-1.4 | `MOCK_WEWORK=false` 下访问 `/mock/wework/users` | HTTP 404 | 单测 |
| TC-MW-1.5 | `MOCK_WEWORK=false` 下调 `WeComClient.send_text` | `_http_post` 被调用 1 次；`mock_outbound_bus.publish` 0 次 | 单测 |

### 4.2 身份入口（字段契约）

| ID | 用例 | 预期 | 类型 |
|---|---|---|---|
| TC-MW-2.1 | `GET /mock/wework/users` | 顶层字段 `== {errcode, errmsg, users}`；`users[]` 每项字段 `== {external_userid, name, role, avatar}` | 单测 |
| TC-MW-2.2 | `GET /mock/wework/users` 响应中不含真实用户 | 所有 `external_userid` 均以 `wm_mock_` 开头 | 单测 |
| TC-MW-2.3 | `GET /mock/wework/oauth2/authorize?appid=X&redirect_uri=https://a.b/cb&state=Z` | HTTP 302；Location 含 `code=MOCK_CODE_xxx&state=Z` | 单测 |
| TC-MW-2.4 | `GET /mock/wework/code2userinfo?access_token=MOCK&code=xxx` | 顶层字段 `== {errcode, errmsg, external_userid, openid}` | 单测 |
| TC-MW-2.5 | `code2userinfo` 响应所有 key 均符合 `[a-z_]+` | regex 断言无大写字符 | 单测 |

### 4.3 入站桥接

| ID | 用例 | 预期 | 类型 |
|---|---|---|---|
| TC-MW-3.1 | `POST /mock/wework/inbound` 完整合法 payload | HTTP 200；顶层 `{errcode:0, errmsg:"ok", msgid}`；DB `wecom_inbound_event` +1；Redis `queue:incoming` +1 | 集成 |
| TC-MW-3.2 | 缺 `MsgId` | `{errcode:40001, errmsg:"missing fields: ['MsgId']"}` | 单测 |
| TC-MW-3.3 | 同 `MsgId` 10 秒内重发 | 第二次 `(duplicate dropped)`；DB 和 Redis 各 1 条 | 集成 |
| TC-MW-3.4 | 同 `MsgId` 跨 Redis TTL（>600s）再发（手工跳过 TTL 或改代码 TTL 到 1s） | 被 DB L2 命中，`(duplicate in db)` | 集成 |
| TC-MW-3.5 | mock 入站构造的 `WeComMessage` 字段结构与 `webhook.py` 解密后 100% 一致 | 字段名集合、类型完全相等（snapshot 对比） | 单测 |
| TC-MW-3.6 | 入队 `queue:incoming` 的 JSON payload 与 `webhook.py` 中一致 | snapshot 断言字段集合 | 单测 |

### 4.4 出站拦截

| ID | 用例 | 预期 | 类型 |
|---|---|---|---|
| TC-MW-4.1 | `MOCK_WEWORK=true` 下调 `send_text("wm_mock_worker_001", "hi")` | `mock_outbound_bus.publish` 1 次；`_http_post` 0 次；返回 `{errcode:0, errmsg:"ok", msgid:"mock_..."}` | 单测 |
| TC-MW-4.2 | 拦截 payload 字段结构 | 顶层 `{touser, msgtype, agentid, text}`；`text` 下有 `content` | 单测 |
| TC-MW-4.3 | `send_text_to_group("gid123", "x")` | `publish` channel `mock:outbound:chat:gid123`；payload 顶层含 `chatid / msgtype / text` | 单测 |
| TC-MW-4.4 | `git diff backend/app/wecom/client.py`（本任务 vs Phase 7 最后 commit） | 只见两段 `if settings.mock_wework:` 新增；原 `_http_post` 调用行 0 改动 | 手工 |
| TC-MW-4.5 | `MOCK_WEWORK=false` 下 TC-MW-4.1 完全反转 | `_http_post` 1 次；`publish` 0 次 | 单测 |

### 4.5 SSE

| ID | 用例 | 预期 | 类型 |
|---|---|---|---|
| TC-MW-5.1 | 建立 SSE，立刻 `publish` 一条 | ≤3s 内收到 `event: message` + 正确 payload | 集成 |
| TC-MW-5.2 | 保持空闲连接 30s | 至少 2 个 `event: ping` | 集成 |
| TC-MW-5.3 | 客户端关闭后 server pubsub 释放 | `PUBSUB NUMSUB mock:outbound:wm_mock_xxx` = 0 | 集成 |
| TC-MW-5.4 | A / B 两个 external_userid 并发 SSE，给 A publish | 只 A 收到，B 不收到 | 集成 |
| TC-MW-5.5 | 入站 → Worker → 出站全链路 | ≤10s 内 SSE 收到 bot 回复；`conversation_log` 新增 in/out 各 1 条 | 集成 |

### 4.6 字段契约一致性（严格锁死）

新建 `backend/tests/unit/test_mock_wework_contract.py`，用 **`==` 严格断言**（不允许 `>=` 或 `issubset`）：

```python
import re

def test_users_schema(client, mock_wework_enabled, mock_users):
    r = client.get("/mock/wework/users")
    data = r.json()
    assert set(data.keys()) == {"errcode", "errmsg", "users"}
    for u in data["users"]:
        assert set(u.keys()) == {"external_userid", "name", "role", "avatar"}
        assert u["external_userid"].startswith("wm_mock_")

def test_code2userinfo_schema(client, mock_wework_enabled):
    r = client.get("/mock/wework/code2userinfo?access_token=x&code=y")
    data = r.json()
    assert set(data.keys()) == {"errcode", "errmsg", "external_userid", "openid"}
    # 所有 key 严格 snake_case
    for k in data.keys():
        assert re.fullmatch(r"[a-z_]+", k), f"key {k!r} violates snake_case"

def test_inbound_required_fields_case(client, mock_wework_enabled, mock_users):
    # 用驼峰而非 Pascal 发送 → 必须失败
    r = client.post("/mock/wework/inbound", json={"touser": "wm_mock_worker_001"})
    assert r.json()["errcode"] == 40001  # 缺所有 Pascal 字段

def test_inbound_shape(client, mock_wework_enabled, mock_users, sample_inbound_payload):
    r = client.post("/mock/wework/inbound", json=sample_inbound_payload)
    data = r.json()
    assert set(data.keys()) == {"errcode", "errmsg", "msgid"}

def test_outbound_payload_shape(mock_wework_enabled, redis_client, mock_users):
    # 订阅后发消息
    pubsub = redis_client.pubsub()
    pubsub.subscribe("mock:outbound:wm_mock_worker_001")
    # 消费第一条 subscribe ack
    pubsub.get_message(timeout=1)
    from app.wecom.client import WeComClient
    WeComClient.from_settings().send_text("wm_mock_worker_001", "hello")
    # 读出拦截的 publish
    msg = None
    for _ in range(20):
        msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg: break
    assert msg is not None, "did not receive publish"
    payload = __import__("json").loads(msg["data"])
    assert set(payload.keys()) == {"touser", "msgtype", "agentid", "text"}
    assert payload["msgtype"] == "text"
    assert set(payload["text"].keys()) == {"content"}

def test_no_userid_field_in_mock_routes(client, mock_wework_enabled, mock_users, sample_inbound_payload):
    """任何 mock 响应中都不应出现 'userid' 字段（企业成员 ID）。"""
    for url in [
        "/mock/wework/users",
        "/mock/wework/code2userinfo?access_token=x&code=y",
    ]:
        r = client.get(url)
        # 深度扫描 JSON 所有 key
        def _all_keys(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield k
                    yield from _all_keys(v)
            elif isinstance(obj, list):
                for x in obj:
                    yield from _all_keys(x)
        keys = set(_all_keys(r.json()))
        assert "userid" not in keys, f"{url} leaked 'userid' field"
```

**这些断言全部使用 `==` 而非 `>=` / `issubset`**，字段集合精确锁定，任何增减都失败。

## 5. 前端测试策略

### 5.1 Vitest 单测（可选）

- `MockIdentityPicker.spec.js`：mock `fetchMockUsers`，渲染后分组正确
- `MockChatPanel.spec.js`：mock `EventSource`，切换身份时旧实例 `close()` 被调用
- `MockBanner.spec.js`：任意 props 下 DOM 都存在 `.mock-banner`

### 5.2 手工 E2E（必做）

见 §8 手工烟雾用例。

## 6. 覆盖目标

| 模块 | 覆盖率目标 |
|---|---|
| `backend/app/api/mock_wework.py` | 行 ≥ 90%，分支 ≥ 85% |
| `backend/app/services/mock_outbound_bus.py` | 行 ≥ 85% |
| `backend/app/wecom/client.py`（本次新增 flag 分支部分） | 行 100%（2 条分支全覆盖） |
| `backend/tests/unit/test_mock_wework_contract.py` | 5 个路由 × 至少 1 个 snapshot 断言 |
| 集成测试 E2E 条数 | ≥ 1 条（入站 → Worker → 出站完整） |

## 7. 回归矩阵

每次改动后必跑（CI 或本地）：

| 测试 | 命令 | 期望 |
|---|---|---|
| 业务单测全集 | `pytest backend/tests/unit/` | 全绿 |
| 集成测试 | `RUN_INTEGRATION=1 pytest backend/tests/integration/` | 全绿 |
| 字段契约 | `pytest backend/tests/unit/test_mock_wework_contract.py -v` | 全绿 |
| Flag 守卫负向 | `ENV=production MOCK_WEWORK=true python -c "from app.config import settings"` | 非 0 退出 |
| Flag 守卫正向 | `ENV=production MOCK_WEWORK=false python -c "from app.config import settings"` | 0 退出 |
| 前端构建 | `cd frontend && npm run build` | 成功 |
| 字段 grep 守卫 | `grep -rn "\"userid\"" backend/app/api/mock_wework.py` | 0 命中 |
| 业务文件 diff 守卫 | `git diff main..HEAD -- backend/app/api/webhook.py backend/app/services/ backend/app/models.py` | 空 |

## 8. 手工烟雾用例

### 8.1 B 模式（双栏）完整对话

1. `.env` 设 `MOCK_WEWORK=true + ENV=development`，`docker compose up -d`
2. 浏览器打开 `/mock`（经 nginx 或 Vite dev）
3. 检查：红色横幅 ✅、双栏布局 ✅
4. 左栏（招聘者）切换到 `wm_mock_factory_001`
5. 右栏（求职者）切换到 `wm_mock_worker_001`
6. 右栏输入"我想找深圳打包工，月薪 5000" → 回车
7. 检查：自己的消息气泡（direction=in）立即显示
8. 观察后端：`conversation_log` +1（direction=in）；Worker 日志消费 `queue:incoming`
9. ≤3s 内 SSE 气泡弹出 bot 回复（direction=out），内容为推荐结果
10. `conversation_log` +1（direction=out）

### 8.2 C 模式（多窗口）演示

1. 窗口 A：`/mock/single?external_userid=wm_mock_factory_001&role=factory`
2. 窗口 B：`/mock/single?external_userid=wm_mock_worker_001&role=worker`
3. 在 A 发送"发布岗位 深圳 打包工 5000" → 后端处理写 `job` 表
4. 在 B 发送"搜索 深圳 打包" → 回复应命中 A 刚发的岗位
5. 刷新 A 和 B，URL 参数保留，身份不丢

### 8.3 字段契约肉眼复核

1. DevTools Network 打开 `POST /mock/wework/inbound` 的 Request Payload
2. 人肉对比与企业微信"接收消息"文档 XML 解密后结构
3. 每个字段名、大小写、嵌套结构必须完全一致

### 8.4 SSE 稳定性长时观察

1. 建立 SSE 连接，保持 30 分钟空闲
2. EventSource 状态 `readyState === 1`（OPEN）持续
3. 至少观察到 120 个 `event: ping`（30 分钟 × 4 个/分钟）
4. 无异常日志
5. 人为切断网络 5s 再恢复：EventSource 自动重连，后续消息正常

## 9. 迁移演练

**目的**：验证主文档 §8 迁移指南实际可行，业务代码 0 改动承诺成立。

**步骤**：
1. 从当前分支切新分支 `mock-migration-dry-run`
2. 按主文档 §8 的 6 步操作（关 flag、删 `main.py` include_router、删 `wecom/client.py` 2 段分支、删 mock 相关新增文件）
3. `git diff main..HEAD -- backend/app/api/webhook.py backend/app/services/` 应为**空**
4. `git diff main..HEAD -- backend/app/models.py backend/app/api/admin/` 应为**空**
5. `git diff main..HEAD -- backend/app/wecom/client.py` 应为**空**（两段分支已删）
6. `pytest backend/tests/` 全绿
7. 验证完毕后切回原分支，该演练分支不 merge

## 10. 性能参考（重要说明）

**本测试台的 SSE 延迟不得作为真企微 P95 回复延迟的验收证据**。原因：

- Mock 走 Redis pub/sub（同机 <1ms），真企微走 HTTPS 外网（数十~数百 ms）
- 入站跳过 AES-CBC + SHA1（省 5~20ms）
- 只有 Worker 消费路径（意图识别 / LLM / 检索）是唯一可参考维度

**可用的内部基线**：
- "入队到 Worker 处理完"这段（业务无关，企微无关）
- LLM 单次调用延迟（不依赖企微）

**不可用**：
- 端到端 P95
- 消息触达率
- 出站失败率（真企微会重试、限流，mock 不会）

## 11. 缺陷分级

| 等级 | 示例 | 处理 |
|---|---|---|
| P0（阻塞） | Flag 守卫失败、字段名不匹配、业务代码被改、真实路径走到 mock 分支 | 立即修 |
| P1（严重） | SSE 断线不重连、幂等失效、前端双栏错位、迁移演练业务代码 diff ≠ 0 | 合并前必修 |
| P2（一般） | 气泡样式微调、切换器过滤异常、ping 间隔偏差 | 可延后 |
| P3（建议） | 性能优化、aioredis 迁移、Vitest 覆盖补齐 | 进 backlog |

## 12. 测试报告产出物

测试完成后在 `collaboration/handoffs/phase7-mock-wework-testbed-test-report.md` 输出：

- 环境信息（OS / 分支 commit / Docker 版本 / Python 版本 / Node 版本）
- 用例执行结果一览表（TC-MW-1.* ~ 5.*）
- 字段契约 snapshot（响应体完整 JSON 贴进文档）
- 双视角 UX 截图（B 模式 + C 模式各 ≥ 1 张）
- SSE 30 分钟稳定性观察结论
- 迁移演练 diff 统计（业务文件行数 = 0 截图）
- 覆盖率报告（pytest-cov）
- P2 / P3 缺陷列表及延后处理建议
