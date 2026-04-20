# Task: Mock 企业微信测试台（Mock-WeWork Testbed）

> 状态：`draft`
> 创建日期：2026-04-19
> 最近更新：2026-04-19
> 任务性质：**独立于 Phase 0~7 业务范围的测试基础设施建设**，与 `phase4-demo-env.md` 互补
> 关联文档：
> - `collaboration/features/phase7-main.md` §0.1 U3（外部企微依赖未闭环）
> - `collaboration/features/phase4-demo-env.md`（自建企微 Demo 的替代备用路径）
> - `collaboration/handoffs/phase7-release-report.md`（最终交付报告中 U3 条目）
> - `方案设计_v0.1.md` §12（企业微信接入）、§17.3（外部依赖确认单）
> - 企业微信官方文档（字段契约来源，见 §3.1 "字段黑话清单"）

---

## 0. 一句话说明

因客户/自建侧暂未提供已备案企业专属域名，无法申请企业微信回调域做真·企微端到端联调。本任务在前端搭建一个"模拟企业微信"的外壳 UI（求职者 + 招聘者双视角、SSE 流式回复），在后端挂一层极薄的"入口 + 桥接"中间层，**下游的 webhook / Worker / message_router / LLM / DB / Redis 队列 100% 走真实数据链路**，把"没有企微就无法验证端到端"这根刺拔掉。

本测试台是**长期存在**的基础设施：即便后续接入真实企业微信，它也继续服役，作为离线回归/冒烟/演示的唯一可靠通道。

---

## 1. 背景与动机

### 1.1 为什么现在要做

- 阶段 0~7 的主链路（企微入站 → Worker → 意图识别 → 检索/上传 → 回复 → 运营后台审核 → 报表）代码已全部完成，Phase 7 文档已可分发。
- `phase7-main.md §0.1` 中 **U3（外部企微 / LLM 依赖）未闭环**，具体阻塞点：企业微信后台要求回调 URL 的主域名必须完成 ICP 备案，备案主体不能是 cpolar / ngrok / natapp 等第三方服务商（`phase4-demo-env.md §暂缓原因`）。
- 客户侧备案域名 + 企微自建应用权限短期内不确定到位时点；内部自购域名备案周期 20~30 天。
- 当前只能通过 `pytest` 单测 + 手写 webhook 包模拟消息，无法体验"用户按企微客户端那样收发消息 + 看到流式回复 + 两端（招聘者/求职者）互动"的完整 UX。
- 真实企微的 P95 / 异常分支 / 角色切换逻辑在"纯后端单测"里暴露不出来。

### 1.2 为什么值得做一个"长期存在"的测试台

- 即便接入真企微之后，出于成本和隐私考虑，也不会在日常开发中反复用真企微压测/回归。
- 真企微接入之后，仍然会有演示、回放、离线冒烟、灰度期外部用户无法进入时的 UI Demo 等场景长期需要这个外壳。
- 把"模拟层"当成临时脚手架会导致字段不一致、上线前翻车；当成长期组件一次性设计好，接入真企微时只是"换一个身份源"。

### 1.3 目标

1. **功能目标**：提供一个在浏览器里就能"扮演任意角色（worker / factory / broker）打开 JobBridge"的测试台，完成从"收到岗位/简历推送 → 输入文本 → 流式看到 bot 回复 → 查看 conversation_log"的闭环。
2. **一致性目标**：测试台往下游发送和接收的数据包，**字段名、字段结构、枚举值与企业微信官方契约 1:1 对齐**，未来接入真企微时只需删除模拟层，业务代码零改动。
3. **安全目标**：通过 `MOCK_WEWORK` feature flag + 环境断言 + UI 顶栏红色横幅，确保生产环境绝不可能误启本测试台。
4. **可维护目标**：一份清单化的"接入真企微时的迁移指南"（§8），让未来接入人员 30 分钟内完成切换。

### 1.4 非目标（本次**不做**）

- ❌ 模拟企业微信"交互卡片"（`template_card`、`card_type`、按钮点击回调）——当前后端 0 处使用，真要用时再补前端渲染 + 回调分支。Payload 字段结构预留，但前端不实现富渲染。
- ❌ 模拟企业微信审批流（`sys_approval_change` 事件、`ApprovalInfo` 明细）——JobBridge 不走审批。
- ❌ 模拟通讯录同步（`change_contact` 事件、`user/get`、`department/list`）——JobBridge 用 `external_userid` 不用 `userid`，无内部员工侧业务。
- ❌ 模拟消息加解密（AES-CBC、`msg_signature`、`EncodingAESKey`）——模拟层直接用明文 JSON，跳过加解密；`backend/app/wecom/crypto.py` 在真企微入口保持不变、正常工作。
- ❌ 模拟富媒体（image / voice / file / video、`media_id` 伪造）——本次只做文本消息；如需扩展，在 §9.2 的"后续增量"里单开任务。
- ❌ 模拟外部联系人选人 JSAPI（`selectEnterpriseContact`、`wx.agentConfig`）——H5 JS-SDK 侧本次不模拟；模拟 UI 直接用身份下拉切换器替代。

---

## 2. 现状盘点

### 2.1 后端已就位部分（可复用，不改动）

| 模块 | 文件 | 现状 |
|---|---|---|
| 企微 API 客户端 | [backend/app/wecom/client.py](../../backend/app/wecom/client.py) | `gettoken / message/send / appchat/send / media/get / externalcontact/get` 全部已实现，token 线程安全缓存。本次只在出站方法里加 `if settings.mock_wework:` 分支，真实调用路径不动。 |
| 加解密 | [backend/app/wecom/crypto.py](../../backend/app/wecom/crypto.py) | SHA1 验签 + AES-CBC 完整实现。模拟层不走这里。 |
| 回调解析 | [backend/app/wecom/callback.py](../../backend/app/wecom/callback.py) | XML → `WeComMessage{msg_id, from_user, to_user, msg_type, content, create_time}`。本次要在模拟入站路由里手动组装同一个 `WeComMessage` 对象。 |
| Webhook 入口 | [backend/app/api/webhook.py](../../backend/app/api/webhook.py) | GET 验证 + POST 消息推送全闭环（验签/幂等/限流/入队）。**真实路径不动**，模拟层新加一条平行路由。 |
| 消息路由 | `backend/app/services/message_router.py` | Worker 消费 `queue:incoming` 后的业务入口，**完全不感知消息来源是真实企微还是模拟**。 |
| 用户识别 | `backend/app/services/user_service.py` — `identify_or_register(from_user, db)` | 基于 `external_userid` 注册或取用户，对消息来源无感。 |
| User 模型 | [backend/app/models.py:19-45](../../backend/app/models.py) | 单表 `user`，PK = `external_userid`；`role` 枚举 `worker / factory / broker`；所有 `Job.owner_userid / Resume.owner_userid` FK 到此。**模拟层的身份只在这张表里选，不新增任何用户表**。 |
| 配置 | [backend/app/config.py:50-55](../../backend/app/config.py) | `wecom_corp_id / wecom_agent_id / wecom_secret / wecom_token / wecom_aes_key` 5 项全空串默认值。本次新增一个 `mock_wework: bool = False` 开关。 |

### 2.2 前端空白（无包袱，从零做）

- [frontend/src/main.js](../../frontend/src/main.js) 不引任何 JS-SDK；项目前端**没有 `wx.config` / `wx.agentConfig` / OAuth 回调页面**。
- 当前前端是 Vue 3 + Element Plus 的**运营后台**，路由全在 `/admin/*` 下；模拟 UI 挂在 `/mock/*` 下独立命名空间，**不与后台路由冲突**，也不消费后台 JWT。
- 登录态/Pinia `authStore` 只服务后台管理员，不参与模拟 UI；模拟 UI 自己用 URL query + sessionStorage 存 `external_userid`，不颁发 JWT。

### 2.3 身份模型（关键）

| 维度 | 真实企业微信 | JobBridge 当前实际使用 | 本测试台采用 |
|---|---|---|---|
| 用户主键 | 内部员工 `userid` / 外部联系人 `external_userid`（`wm` 前缀） | **全量外部联系人（`external_userid`）** | 照搬：`external_userid`，`wm_mock_xxxxxx` 前缀，不与真实 `wm` ID 冲突 |
| 角色分野 | 通讯录部门、`isleader` 等 | `user.role` 枚举：`worker / factory / broker` | 照搬，UI 身份切换器按 `role` 分组 |
| 授权流程 | OAuth2 + `snsapi_base` / `snsapi_privateinfo` | 不实现 | 模拟版：`/mock/wework/oauth2/authorize` 直接跳转，`/mock/wework/code2userinfo` 返回企微格式 JSON |

**结论**：模拟层**只覆盖单一身份域（`external_userid`）**，不碰内部员工 `userid`。未来若 JobBridge 要扩到内部员工侧，再单开任务。

### 2.4 消息格式现状

- 后端 `WeComClient.send_text / send_text_to_group` 目前只发纯文本（`msgtype=text`）。
- `message_router` 输出的 `ReplyMessage` 目前也只有 `content`（字符串），**没有 `template_card` 分支**，所以模拟层的出站端只需要能渲染 `msgtype=text` 即可；但出站 payload 的顶层结构仍按企微契约保留 `{msgtype, agentid, touser, text.content}` 完整结构，供未来扩展。

---

## 3. 设计原则

### 3.1 字段黑话清单锁死（最重要）

以下字段名**原样使用**，不驼峰化、不翻译、不改结构。任何改名都是给未来接入真企微埋雷。

| 类别 | 必须保留的字段 |
|---|---|
| 身份 | `corpid`、`agentid`、`userid`、`open_userid`、**`external_userid`**（`wm` 前缀）、`user_ticket`、`unionid` |
| OAuth2 | `appid`(=corpid)、`response_type=code`、`scope=snsapi_base / snsapi_privateinfo`、`state`、`agentid` |
| 回调 XML 对应字段 | `ToUserName`(=corpid)、`FromUserName`、`AgentID`、`MsgType`、`Event`、`ChangeType`、`Encrypt`、`msg_signature / timestamp / nonce / echostr` |
| 消息下发 | `msgtype`、`agentid`、`touser / toparty / totag`（**`|` 连接字符串，不是数组**）、`safe (0/1/2)`、`enable_id_trans`、`enable_duplicate_check`、`msgid`、`invaliduser`、`unlicenseduser` |
| JS-SDK（本次不做，但字段先占位） | `wx.config` 的 `appId`（驼峰，值是 corpid）、`wx.agentConfig` 的 `corpid`（全小写）——两者故意不一样，不要统一 |
| 通用返回 | 顶层恒为 `errcode / errmsg` |

**三个最容易翻车的点**（开发时贴眼前）：

1. `touser / toparty / totag` 在 `/cgi-bin/message/send` 是 **`|` 连接的字符串**，不是 JSON 数组。模拟 UI 的后端解析也按字符串切分。
2. `wx.config` 的 key 是 `appId`（驼峰），`wx.agentConfig` 的 key 是 `corpid`（全小写）——接入真企微时两套签名接口的入参大小写**故意不一样**，别擅自统一。
3. 企业成员 `userid` ≠ 外部联系人 `external_userid`；JobBridge 面向候选人/招聘者，全程走 `external_userid` + `/cgi-bin/externalcontact/get`。模拟层所有路径参数/请求体都只用 `external_userid`，**不要出现 `userid`** 这个字段名（哪怕业务语义通）。

### 3.2 切面最小化

模拟层**只在两处切**，其它地方一个字符都不动：

1. **身份入口**：`/mock/wework/*` 路由（前端伪装"在企业微信里打开" + 后端伪 OAuth 换 `external_userid`）
2. **出站拦截**：`WeComClient.send_text / send_text_to_group` 方法内部加一个 `if settings.mock_wework:` 分支，把要发给企微的 payload 写入 Redis 专用 channel，真调用路径短路

其它一切（webhook 真实路径、crypto、callback、Worker、message_router、service、LLM、DB 查询、Redis 队列）全部不动。

### 3.3 Feature Flag 强制隔离

新增 `MOCK_WEWORK: bool = False`（`.env`），启用时：

1. `/mock/*` 路由**仅在 `MOCK_WEWORK=true` 时注册**（`main.py` 条件 `include_router`）；生产环境即便配置被误改，路由根本不存在，返回 404
2. 应用启动时 assert：`if settings.mock_wework and settings.env == "production": raise RuntimeError`
3. 前端模拟 UI 顶部挂**醒目红色横幅**"MOCK-WEWORK TESTBED — 非真实企业微信环境"，防止截图误导
4. 出站拦截只在 `settings.mock_wework` 为真时短路；默认真调企微 API

### 3.4 接入真实企业微信时的删除清单（提前设计）

本节和 §8 互为参考，确保模拟层**可一键剥离**：

- 删 `/mock/*` 路由注册
- 删 `WeComClient` 里的两个 `if settings.mock_wework:` 分支
- 删前端 `/mock` 路由 + 相关组件目录
- 把 `.env` 里 `MOCK_WEWORK=true` 改为 `false` 或直接删除
- 业务代码（webhook / Worker / services / models / admin API）**零改动**

---

## 4. 架构方案

### 4.1 整体数据流

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │  浏览器（模拟 UI，两种打开方式：左右分栏 OR 多窗口多标签）             │
 │                                                                      │
 │   [招聘者视角]          ←→          [求职者视角]                       │
 │   external_userid=                  external_userid=                 │
 │   wm_mock_factory_001               wm_mock_worker_001               │
 │        ▲      │                            ▲      │                  │
 │   SSE  │      │POST inbound         SSE    │      │POST inbound       │
 └────────┼──────┼────────────────────────────┼──────┼──────────────────┘
          │      │                            │      │
 ┌────────┼──────┼────────────────────────────┼──────┼──────────────────┐
 │        │      ▼                            │      ▼                   │
 │   /mock/wework/sse          /mock/wework/inbound                      │
 │        ▲                              │                               │
 │        │                              │ 构造 WeComMessage             │
 │        │                              ▼                               │
 │        │                   Redis queue:incoming                       │
 │        │                              │                               │
 │        │                              ▼                               │
 │        │                    Worker 进程（真实）                        │
 │        │                              │                               │
 │        │                              ▼                               │
 │        │                 message_router / services（真实）             │
 │        │                              │                               │
 │        │                              ▼                               │
 │        │            WeComClient.send_text(to_user, content)           │
 │        │                              │                               │
 │        │           if settings.mock_wework:                           │
 │        │               ▼                                              │
 │        └─── Redis pub/sub: mock:outbound:{external_userid}            │
 │                                                                      │
 │   FastAPI 后端（真实 DB / Redis / LLM）                                │
 └──────────────────────────────────────────────────────────────────────┘
```

### 4.2 入口伪装（替代 OAuth2 + JS-SDK）

**前端路由**（新增，`MOCK_WEWORK=true` 时挂载）：

| 路径 | 组件 | 用途 |
|---|---|---|
| `/mock` | `MockEntryView.vue` | 模拟 UI 入口；URL 带 `?external_userid=...&role=...` 时直接进单视角模式，不带参数时进双栏默认模式 |
| `/mock/split` | `MockSplitView.vue` | 左右分栏双视角（默认） |
| `/mock/single` | `MockSingleView.vue` | 单视角（用于多窗口多标签 C 模式） |

**后端路由**（新增）：

| 方法 + 路径 | 用途 | 返回 |
|---|---|---|
| `GET /mock/wework/users` | 拉可选身份列表（供切换器下拉） | `{errcode, errmsg, users: [{external_userid, name, role, avatar}]}` |
| `GET /mock/wework/oauth2/authorize` | 伪 OAuth2 授权跳转（仅用于演示字段形状） | 302 → `redirect_uri?code=MOCK_CODE_xxx&state=...` |
| `GET /mock/wework/code2userinfo` | 伪 code 换 userinfo，**字段完全照搬企微** | `{errcode:0, errmsg:"ok", external_userid:"wm_mock_xxx", openid:"..."}` |

### 4.3 入站桥接（替代 webhook POST）

**新增 `POST /mock/wework/inbound`**（仅 `MOCK_WEWORK=true` 注册）：

请求体（**字段名对齐官方解密后 XML 的 JSON 等价结构**）：

```json
{
  "ToUserName":   "wwmock_corpid",
  "FromUserName": "wm_mock_worker_001",
  "CreateTime":   1713500000,
  "MsgType":      "text",
  "Content":      "我想找深圳的打包工",
  "MsgId":        "mock_msgid_1713500000_abc",
  "AgentID":      "1000002"
}
```

服务端处理：
1. 跳过 SHA1 验签 + AES 解密（模拟层明文）
2. 其它路径**100% 复用 webhook.py 的真实代码**：
   - 构造 `WeComMessage` 对象（`backend/app/wecom/callback.py`）
   - Redis L1 + DB L2 幂等检查（按 `MsgId`）
   - 限流（按 `FromUserName` + 配置阈值）
   - 写 `wecom_inbound_event` 表
   - `rpush queue:incoming`
3. 返回 `{"errcode":0,"errmsg":"ok"}` + HTTP 200

### 4.4 出站拦截 + SSE 流式回复

**在 `WeComClient.send_text / send_text_to_group` 内部加分支**（唯一侵入真实代码的点）：

```python
# backend/app/wecom/client.py — 伪代码
def send_text(self, touser: str, content: str) -> dict:
    payload = {
        "touser": touser,
        "msgtype": "text",
        "agentid": self.agentid,
        "text": {"content": content},
    }
    if settings.mock_wework:
        # 出站拦截：payload 字段名保留企微契约，直接推 Redis pub/sub
        redis.publish(f"mock:outbound:{touser}", json.dumps(payload))
        return {"errcode": 0, "errmsg": "ok", "msgid": f"mock_{uuid4().hex}"}
    # 真实调用路径（当前代码，不动）
    return self._http_post("/cgi-bin/message/send", json=payload)
```

**SSE 端点 `GET /mock/wework/sse?external_userid=wm_mock_xxx`**（仅 `MOCK_WEWORK=true` 注册）：
- `Content-Type: text/event-stream`
- 订阅 Redis `mock:outbound:{external_userid}` channel
- 每收到一条出站 payload，`yield data: {JSON}\n\n` 推给前端
- 连接保活：每 15s 发一条 `event: ping` 注释帧
- 前端断线自动重连（`EventSource` 原生支持）

**关于"流式回复"**：真实企微 `/cgi-bin/message/send` 是**整条整条发**的（不是 token 流），所以 SSE 推送也是**整条整条推**。如果未来想要"LLM 生成中逐 token 显示"的效果，那是**LLM 层**的流式（在 `message_router` 或 `llm/providers` 里实现），再把每个 token 作为一条独立的出站 payload 发给模拟 UI 即可——不在本任务范围内，但 SSE 通道本身已为它预留。

### 4.5 双视角 UX（B + C 并存）

**B 模式 — 左右分栏（默认）**：
- 访问 `/mock` 或 `/mock/split`
- 页面左半"招聘者"、右半"求职者"，各自独立聊天窗 + 身份切换器 + SSE 连接
- 切换器顶部有搜索框，可按 `external_userid` / `name` 过滤候选身份
- 每个面板独立订阅自己的 Redis channel

**C 模式 — 多窗口多标签**：
- 访问 `/mock/single?external_userid=wm_mock_worker_001&role=worker`
- 只渲染单一视角，适合开多个浏览器窗口各扮演一个角色，做"招聘者发岗 → 求职者立刻收到推荐"的演示
- URL 本身就是状态，支持书签、分享

**共用组件**：
- `MockIdentityPicker.vue`：身份切换器（按 `role` 分组）
- `MockChatPanel.vue`：单视角聊天窗（消息列表 + 输入框 + SSE 订阅）
- `MockBanner.vue`：顶部红色警告横幅

---

## 5. 接口契约（完整）

### 5.1 身份与入口

**`GET /mock/wework/users`** — 拉切换器下拉选项

```json
// Response
{
  "errcode": 0,
  "errmsg": "ok",
  "users": [
    {"external_userid": "wm_mock_worker_001",  "name": "张工", "role": "worker",  "avatar": "..."},
    {"external_userid": "wm_mock_factory_001", "name": "华东电子厂", "role": "factory", "avatar": "..."},
    {"external_userid": "wm_mock_broker_001",  "name": "李中介", "role": "broker",  "avatar": "..."}
  ]
}
```

**`GET /mock/wework/oauth2/authorize?appid=...&redirect_uri=...&response_type=code&scope=snsapi_base&agentid=...&state=...`**

→ 302 `Location: {redirect_uri}?code=MOCK_CODE_{uuid}&state={state}`

**`GET /mock/wework/code2userinfo?access_token=MOCK&code=MOCK_CODE_xxx`**

```json
// Response（字段名完全照搬官方 /cgi-bin/auth/getuserinfo）
{
  "errcode": 0,
  "errmsg": "ok",
  "external_userid": "wm_mock_worker_001",
  "openid": "mock_openid_xxx"
}
```

### 5.2 入站

**`POST /mock/wework/inbound`**

请求体见 §4.3。响应：`{"errcode":0,"errmsg":"ok"}`。

### 5.3 出站（SSE）

**`GET /mock/wework/sse?external_userid=wm_mock_xxx`**

推送帧（`data:` 部分为 JSON，字段名与 `/cgi-bin/message/send` 请求体完全一致）：

```
event: message
data: {"touser":"wm_mock_worker_001","msgtype":"text","agentid":"1000002","text":{"content":"..."}}

event: ping
data: {"ts": 1713500000}
```

---

## 6. 改动范围（文件级清单）

### 6.1 后端新增

| 文件 | 职责 |
|---|---|
| `backend/app/api/mock_wework.py` | 所有 `/mock/wework/*` 路由（users / authorize / code2userinfo / inbound / sse） |
| `backend/app/services/mock_outbound_bus.py` | Redis pub/sub 封装（publish / subscribe / SSE 迭代器） |
| `backend/sql/seed_mock_users.sql` | 幂等 INSERT 若干 `wm_mock_*` 用户，覆盖 worker / factory / broker 三种 role |
| `backend/tests/unit/test_mock_wework.py` | 模拟 UI 路由 + 出站拦截单测 |

### 6.2 后端修改（侵入真实代码的唯二处）

| 文件 | 改动 |
|---|---|
| [backend/app/config.py](../../backend/app/config.py) | 新增 `mock_wework: bool = False` + 启动期 assert（prod 下禁用） |
| [backend/app/wecom/client.py](../../backend/app/wecom/client.py) | `send_text` / `send_text_to_group` 内部加 `if settings.mock_wework:` 分支推 Redis，真实路径不动 |
| [backend/app/main.py](../../backend/app/main.py) | 条件 `include_router(mock_wework.router)` — 仅 `MOCK_WEWORK=true` 时注册 |
| [.env.example](../../.env.example) | 新增 `MOCK_WEWORK=false` 一行 + 注释 |

### 6.3 前端新增

| 文件 | 职责 |
|---|---|
| `frontend/src/views/mock/MockEntryView.vue` | `/mock` 入口，根据 URL query 分流到 split / single |
| `frontend/src/views/mock/MockSplitView.vue` | 左右分栏双视角 |
| `frontend/src/views/mock/MockSingleView.vue` | 单视角（多窗口模式） |
| `frontend/src/components/mock/MockIdentityPicker.vue` | 身份切换器，按 `role` 分组 |
| `frontend/src/components/mock/MockChatPanel.vue` | 单视角聊天窗 + SSE 订阅 |
| `frontend/src/components/mock/MockBanner.vue` | 顶部红色警告横幅 |
| `frontend/src/api/mock.js` | 封装 `/mock/wework/*` HTTP 调用 + `EventSource` 管理 |
| `frontend/src/router/index.js`（修改） | 新增 `/mock/*` 路由，**不走 `auth.isAuthenticated` 守卫** |

### 6.4 文档

| 文件 | 内容 |
|---|---|
| `collaboration/features/phase7-mock-wework-testbed.md`（本文件） | 总体设计 |
| `collaboration/features/phase7-mock-wework-testbed-dev-checklist.md` | 开发执行清单（施工完成后产出） |

---

## 7. 做到什么程度（验收标准）

### 7.1 功能验收

- [ ] F1：`MOCK_WEWORK=true` 启动后，访问 `/mock` 能看到双栏 UI 与红色横幅
- [ ] F2：身份切换器能列出 seed 的全部 `wm_mock_*` 用户，按 `role` 分组
- [ ] F3：在任一面板输入文本并发送，后端能在 `conversation_log` 中看到 `direction=in` 记录，且 Worker 正常消费
- [ ] F4：Worker 处理完后的 bot 回复能在 2 秒内以 SSE 推送到模拟 UI，气泡正常渲染
- [ ] F5：招聘者视角发岗后，若后端匹配逻辑生成"向候选人推送岗位"的消息，求职者视角能收到
- [ ] F6：URL 带 `?external_userid=...&role=...` 参数能直接进单视角，用于多窗口演示
- [ ] F7：SSE 断线后 `EventSource` 自动重连，消息不丢

### 7.2 字段一致性验收

- [ ] C1：`/mock/wework/code2userinfo` 返回体首层字段为 `{errcode, errmsg, external_userid, openid}`，字段名 0 改动
- [ ] C2：`/mock/wework/inbound` 请求体顶层字段为 `{ToUserName, FromUserName, CreateTime, MsgType, Content, MsgId, AgentID}`，大小写完全照搬 XML 标签
- [ ] C3：SSE 推送的出站 payload 顶层字段为 `{touser, msgtype, agentid, text}`（或未来的 `template_card` / `image` 等），`touser` 在群消息场景用 `|` 分隔字符串，不是数组
- [ ] C4：`backend/app/wecom/client.py` 的 `_http_post` 真实路径 0 改动（diff 只在 `if settings.mock_wework:` 分支）

### 7.3 安全验收

- [ ] S1：`ENV=production` + `MOCK_WEWORK=true` 启动时，应用直接 `RuntimeError` 拒绝启动
- [ ] S2：`ENV=production` + `MOCK_WEWORK=false` 启动时，`/mock/*` 全部 404
- [ ] S3：前端模拟 UI 页面顶部必出红色横幅，颜色和文本不可通过 URL 参数隐藏
- [ ] S4：模拟 UI 不颁发 JWT，不访问 `/admin/*` API，不污染运营后台 session

### 7.4 可迁移验收

- [ ] M1：按 §8 迁移指南操作，能在 30 分钟内把代码切换到"模拟层完全移除"状态
- [ ] M2：移除模拟层后，`pytest` 全套单测通过，业务代码 diff 行数为 0
- [ ] M3：所有字段名对齐性单测（§7.2）在迁移前后结果一致（意味着契约没变）

---

## 8. 接入真实企业微信时的迁移指南

**前置**：企业专属域名已备案、企业微信自建应用已创建、`.env` 里 `WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET / WECOM_TOKEN / WECOM_AES_KEY` 五项全部填好、回调 URL 在企微后台配置完毕。

**操作步骤**（30 分钟内完成）：

1. **关闭 flag**：`.env` 设 `MOCK_WEWORK=false`
2. **删路由注册**：`backend/app/main.py` 去掉 `include_router(mock_wework.router)` 分支
3. **删拦截分支**：`backend/app/wecom/client.py` 中 `send_text / send_text_to_group` 内部的 `if settings.mock_wework:` 两段分支整块删除
4. **删模拟层源码**（可选，建议保留做回归）：
   - `backend/app/api/mock_wework.py`
   - `backend/app/services/mock_outbound_bus.py`
   - `frontend/src/views/mock/` 整个目录
   - `frontend/src/components/mock/` 整个目录
   - `frontend/src/api/mock.js`
   - 前端 `/mock/*` 路由注册
5. **保留**：
   - `backend/sql/seed_mock_users.sql`（测试环境依然好用）
   - `backend/tests/unit/test_mock_wework.py`（回归依然跑）
6. **验证**：`pytest` 全套 + 真企微链路冒烟（企微后台给应用发一条文本消息，观察 `conversation_log` + 出站真实触达）

**原则**：业务代码（webhook 真实路径、Worker、services、models、admin API、report、audit）**不动任何一个字符**。如果迁移时发现有业务代码需要改，说明模拟层设计出了问题，应该退回本文档修正而不是在迁移侧打补丁。

---

## 9. 后续注意事项

### 9.1 不模拟的场景清单（未来要用时需单开任务）

| 场景 | 未来扩展位置 |
|---|---|
| 交互卡片（`template_card` 5 种 `card_type`） | `MockChatPanel.vue` 加卡片渲染组件；后端 `WeComClient` 加 `send_template_card`；新增 `/mock/wework/card_callback` 路由回调 `response_code` |
| 富媒体消息（image / voice / file / video） | 模拟 UI 加上传控件；后端加 `media_id` 伪造器；`send_image / send_voice / send_file / send_video` 出站拦截 |
| 审批流（`sys_approval_change`） | 新增 `/mock/wework/approval/*` 路由族 |
| 通讯录事件（`change_contact`） | 新增 `/mock/wework/contact/*` 路由族 + 模拟员工入离职页面 |
| JS-SDK（`wx.config / wx.agentConfig / selectEnterpriseContact`） | 引入企微 JS-SDK 真实 SDK；后端 `/mock/wework/jssdk/signature` 返回伪签名 |
| UnionID ↔ external_userid 反查 | `GET /mock/wework/unionid_to_external_userid` |

### 9.2 已知风险

| 风险 | 缓解 |
|---|---|
| **生产误启**：运维改 `.env` 时手误把 `MOCK_WEWORK` 设 `true` | 应用启动 assert + 前端红色横幅 + `/mock/*` 路由仅 flag 为真时注册（三重保险） |
| **字段漂移**：企业微信官方文档后续更新字段名或新增必填字段 | 每季度盘一次契约；所有字段名单测（§7.2）由 CI 强制通过 |
| **性能对比失真**：模拟层走 Redis pub/sub，时延与真企微 `/cgi-bin/message/send` 不同 | P95 回复延迟的正式压测**必须**在真企微环境跑，不用模拟层数据作为性能验收证据 |
| **幂等边界差异**：真企微重试靠 `msg_signature + MsgId`，模拟层直接用 `MsgId` | 模拟入站接口文档明确提醒"测试幂等时手动构造相同 `MsgId`"；幂等单测已有，不退化 |
| **回调加解密路径不走**：模拟层跳过 `backend/app/wecom/crypto.py` | 真企微上线前必跑一次 `backend/tests/unit/test_wecom_crypto.py`（已有）+ 真机冒烟验签 |
| **通讯录/审批盲区**：本测试台不模拟这两类事件 | §9.1 已列出未来扩展路径；上线前如需用到，单开任务不要在本任务内加塞 |

### 9.3 长期维护建议

1. **季度盘点**：每季度检查一次企业微信官方文档是否有字段新增/废弃，更新本文档 §3.1 和 §7.2。
2. **Seed 数据治理**：`seed_mock_users.sql` 里的 `wm_mock_*` 用户不要和真实数据混库；生产库若误导入必须能一键清理（可加 `WHERE external_userid LIKE 'wm_mock_%'` 清理脚本）。
3. **模拟层代码归属**：本任务产出的 `backend/app/api/mock_wework.py` + 前端 `mock/` 目录，归属"测试基础设施"类别，不计入业务代码 LOC；后续业务 PR 不得在这些文件里加业务逻辑。
4. **文档同步**：接入真企微后，本文档不删除，但新增"迁移完成回顾"一节，记录真实接入过程中遇到的字段契约坑点，供其它项目参考。
5. **与 `phase4-demo-env.md` 的关系**：该文档里规划的"自建 Demo 企微"一旦未来真跑通，优先级应高于本测试台；本测试台退化为"离线冒烟/演示"通道，但**不应被删除**——它填补的是"真企微不可用或不便使用时"的永久空窗。

---

## 10. 修订记录

| 日期 | 版本 | 变更 | 作者 |
|---|---|---|---|
| 2026-04-19 | draft-v1 | 初稿。基于本次代码现状调研 + 企微官方接口调研产出。确定"求职者 + 招聘者双视角、B+C UX、SSE 流式、不做交互卡片"四项产品决策。 | Claude + songyanbei |

---

## 附录 A：术语与缩写

| 术语 | 含义 |
|---|---|
| **测试台 / Testbed** | 本任务产出的模拟企业微信 UI + 后端桥接层，长期存在 |
| **切面（seam）** | 模拟层侵入真实代码的位置，本测试台只允许两个切面（入口 + 出站拦截） |
| **黑话清单** | 企业微信官方契约字段名，不得改动翻译的那批字段（§3.1） |
| **Flag** | `MOCK_WEWORK` 环境变量布尔开关 |
| **出站拦截** | `WeComClient.send_*` 方法里对出向企微 API 的调用做短路 |
| **入站桥接** | `/mock/wework/inbound` 路由把模拟 UI 的用户输入转成 `WeComMessage` 投入真实队列 |
