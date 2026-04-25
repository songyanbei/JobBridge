# Mock 企业微信测试台 开发实施文档

> 基于：`collaboration/features/phase7-mock-wework-testbed.md`
> 配套 Checklist：`collaboration/features/phase7-mock-wework-testbed-dev-checklist.md`
> 面向角色：后端 + 前端开发
> 状态：`draft`
> 创建日期：2026-04-19

## 1. 开发目标

在**不改动任何业务代码**（`api/webhook.py` 真实路径 / Worker / `services/*` / `models.py` / `admin/*`）的前提下，新增一层由 `MOCK_WEWORK=true` 启用、生产环境强制禁用的 Mock 企业微信测试台。产出物：

- 后端：5 个 `/mock/wework/*` 路由 + Redis pub/sub 出站总线 + `WeComClient` 两处拦截分支
- 前端：3 个 `/mock/*` 页面 + 3 个共用组件 + 1 个 API 封装
- 配置：`MOCK_WEWORK` flag + 启动期 env 断言
- SQL：`seed_mock_users.sql` 幂等 seed 至少 3 个 `wm_mock_*` 用户
- 测试：字段契约单测 + flag 隔离单测（详见 `phase7-mock-wework-testbed-test-implementation.md`）

## 2. 当前代码现状（开工前必读）

- [backend/app/wecom/client.py](../../backend/app/wecom/client.py) 已有 `send_text(touser, content)` 与 Phase 7 新增的 `send_text_to_group(chat_id, content)`；本任务**不新增方法**，只在方法体入口加 flag 分支
- [backend/app/api/webhook.py](../../backend/app/api/webhook.py) 是真实企微入口，包含"验签 → 解密 → 构造 `WeComMessage` → 幂等 → 限流 → 写 `wecom_inbound_event` → rpush `queue:incoming`"完整链路；本任务 `/mock/wework/inbound` 要**复用**该路径"构造 `WeComMessage` 起之后"的所有步骤
- [backend/app/wecom/callback.py](../../backend/app/wecom/callback.py) 的 `WeComMessage` dataclass 就是队列内传输的数据结构，mock 入站必须构造同形结构
- [backend/app/models.py:19-45](../../backend/app/models.py) `user` 表 PK = `external_userid`、`role` 枚举 `worker/factory/broker`；模拟身份直接 INSERT 到此表，**不新增表**
- [backend/app/main.py](../../backend/app/main.py) 当前注册 admin / webhook / events 三组路由
- [frontend/src/router/index.js](../../frontend/src/router/index.js) 的 `router.beforeEach` 要求 `auth.isAuthenticated`；`/mock/*` 必须跳过
- [.env.example](../../.env.example) 已有 `WECOM_*` 5 项

如现状与上述不符，在 `collaboration/handoffs/` 记录差异再决定是否调整实施范围。

## 3. 开发原则

### 3.1 切面最小化：只允许两处切口

- **切口 A：身份入口** — 新建 `backend/app/api/mock_wework.py`（全新文件，0 侵入）
- **切口 B：出站拦截** — `backend/app/wecom/client.py` 的 `send_text` 与 `send_text_to_group` 方法体入口各加一段 `if settings.mock_wework:` 分支，**真实调用路径（`self._http_post`）diff 行数必须为 0**

除以上两处外，业务文件（`services / models / api/admin/* / api/webhook.py / message_router / Worker`）diff 行数必须为 0。PR review 见到其它文件被改直接打回。

### 3.2 字段黑话锁死

所有 mock 接口请求/响应/SSE 帧字段名与企微官方契约 1:1 对齐。参考主文档 §3.1。三个最易翻车点：

1. `touser / toparty / totag` 在 `/cgi-bin/message/send` 是 `|` 连接**字符串**，不是数组
2. `wx.config` 的 `appId`（驼峰）≠ `wx.agentConfig` 的 `corpid`（全小写）——两者故意不同
3. JobBridge 全程走 `external_userid`，mock 层**不得出现 `userid` 字段**

### 3.3 Feature Flag 三重隔离

1. **路由注册层**：`main.py` 中 `if settings.mock_wework: app.include_router(...)` — flag 为 False 时路由根本不注册
2. **启动期 assert**：`config.py` 的 `@model_validator` 校验 `env=="production" and mock_wework==True` 组合则 `raise RuntimeError`
3. **前端横幅**：`MockBanner.vue` 无 props、无条件渲染开关，硬编码

### 3.4 前后端不共享 session

- 模拟 UI **不颁发 JWT**，不使用 `authStore`，不调 `/admin/*`
- 身份信息只存于 URL query（`?external_userid=...&role=...`）+ sessionStorage
- `/mock/*` 路由不强制鉴权，靠 flag + CORS 隔离

## 4. 开发顺序建议

1. **配置 + Flag**：`config.py` + `.env.example`
2. **DB seed**：`seed_mock_users.sql`
3. **出站总线**：`services/mock_outbound_bus.py`
4. **出站拦截**：`wecom/client.py` 两处加分支 + 单测
5. **后端 mock 路由**：`api/mock_wework.py`（users → authorize → code2userinfo → inbound → sse）
6. **条件注册**：`main.py`
7. **前端路由**：`router/index.js`（含 `skipAuth` 守卫）
8. **前端组件**：`MockBanner` → `MockIdentityPicker` → `MockChatPanel` → `MockSplitView` / `MockSingleView` / `MockEntryView`
9. **前端 API 层**：`api/mock.js`
10. **本地联调**：双栏手动跑"招聘者发岗 → 求职者看到推荐"

## 5. 后端模块实现

### 5.1 `backend/app/config.py` — Flag 与启动 assert

```python
from pydantic import Field, model_validator

# 在 Settings 类中新增字段
mock_wework: bool = Field(default=False, alias="MOCK_WEWORK")

# 新增 validator
@model_validator(mode="after")
def _guard_mock_wework(self) -> "Settings":
    if self.mock_wework and self.env == "production":
        raise RuntimeError(
            "MOCK_WEWORK=true is forbidden in production. "
            "Set MOCK_WEWORK=false or switch ENV away from production."
        )
    return self
```

### 5.2 `backend/app/api/mock_wework.py`

所有返回体顶层恒有 `errcode / errmsg`。路由前缀 `/mock/wework`。

#### 5.2.1 路由骨架

```python
import json
import secrets
import asyncio
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from loguru import logger

from app.api.deps import get_db
from app.models import User, WecomInboundEvent
from app.wecom.callback import WeComMessage
from app.core.redis_client import get_redis
from app.services import mock_outbound_bus

router = APIRouter(prefix="/mock/wework", tags=["mock-wework"])
```

#### 5.2.2 `GET /users` — 身份切换器数据源

```python
@router.get("/users")
def list_mock_users(db: Session = Depends(get_db)):
    rows = db.query(User).filter(User.external_userid.like("wm_mock_%")).all()
    return {
        "errcode": 0,
        "errmsg": "ok",
        "users": [
            {
                "external_userid": u.external_userid,
                "name": u.display_name or u.external_userid,
                "role": u.role,
                "avatar": (u.extra or {}).get("avatar", ""),
            }
            for u in rows
        ],
    }
```

**注意**：只查 `wm_mock_%` 前缀，绝不暴露真实用户到模拟 UI。

#### 5.2.3 `GET /oauth2/authorize` — 伪 OAuth2 跳转

```python
@router.get("/oauth2/authorize")
def mock_authorize(
    appid: str,
    redirect_uri: str,
    response_type: str = "code",
    scope: str = "snsapi_base",
    agentid: str | None = None,
    state: str | None = None,
):
    # 不校验 appid / agentid，纯字段透传演示
    code = f"MOCK_CODE_{secrets.token_hex(8)}"
    qs = {"code": code}
    if state:
        qs["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}{urlencode(qs)}"
    return RedirectResponse(target, status_code=302)
```

#### 5.2.4 `GET /code2userinfo` — 字段形态完全照搬

```python
@router.get("/code2userinfo")
def mock_code2userinfo(access_token: str, code: str, request: Request):
    # 本测试台用 URL 上附加的 x_mock_external_userid 作身份源
    # （未来接入真企微时，此路由整个被替换为 /cgi-bin/auth/getuserinfo）
    external_userid = request.query_params.get("x_mock_external_userid", "wm_mock_worker_001")
    return {
        "errcode": 0,
        "errmsg": "ok",
        "external_userid": external_userid,
        "openid": f"mock_openid_{external_userid}",
    }
```

#### 5.2.5 `POST /inbound` — 入站桥接（核心）

```python
@router.post("/inbound")
def mock_inbound(payload: dict, db: Session = Depends(get_db)):
    # 1. 字段校验（按企微 XML 解密后结构命名，大小写照搬）
    required = ["ToUserName", "FromUserName", "CreateTime", "MsgType", "Content", "MsgId", "AgentID"]
    missing = [k for k in required if k not in payload]
    if missing:
        return {"errcode": 40001, "errmsg": f"missing fields: {missing}"}

    # 2. 构造 WeComMessage（与 webhook.py 真实解密后结构一致）
    msg = WeComMessage(
        msg_id=str(payload["MsgId"]),
        from_user=str(payload["FromUserName"]),
        to_user=str(payload["ToUserName"]),
        msg_type=str(payload["MsgType"]),
        content=str(payload["Content"]),
        create_time=int(payload["CreateTime"]),
    )

    # 3. 幂等：L1 Redis（600s）+ L2 DB
    r = get_redis()
    idempotent_key = f"wecom:msg:{msg.msg_id}"
    if not r.set(idempotent_key, "1", nx=True, ex=600):
        return {"errcode": 0, "errmsg": "ok (duplicate dropped)"}
    if db.query(WecomInboundEvent).filter_by(msg_id=msg.msg_id).first():
        return {"errcode": 0, "errmsg": "ok (duplicate in db)"}

    # 4. 写 wecom_inbound_event（与 webhook.py 字段一致）
    event = WecomInboundEvent(
        msg_id=msg.msg_id,
        from_userid=msg.from_user,
        msg_type=msg.msg_type,
        raw_payload=json.dumps(payload, ensure_ascii=False),
    )
    db.add(event)
    db.commit()

    # 5. 入队（payload 结构和 webhook.py 里一致）
    queue_payload = {
        "msg_id": msg.msg_id,
        "from_user": msg.from_user,
        "to_user": msg.to_user,
        "msg_type": msg.msg_type,
        "content": msg.content,
        "create_time": msg.create_time,
    }
    r.rpush("queue:incoming", json.dumps(queue_payload))
    return {"errcode": 0, "errmsg": "ok", "msgid": msg.msg_id}
```

**与真实 webhook.py 的唯一差异**：跳过"验签 + 解密"。其它步骤**必须字段级一致**。建议后续重构时把"幂等 + 写 event + 入队"抽出成 `services/inbound_ingest.py` 供共享（可选，不做也可；但测试需通过 snapshot 强一致性）。

#### 5.2.6 `GET /sse` — SSE 推送

```python
@router.get("/sse")
async def mock_sse(external_userid: str):
    async def event_stream():
        pubsub = mock_outbound_bus.subscribe(external_userid)
        try:
            yield f"event: ready\ndata: {{\"external_userid\":\"{external_userid}\"}}\n\n"
            async for frame in mock_outbound_bus.iter_frames(pubsub):
                yield frame
        finally:
            await mock_outbound_bus.unsubscribe(pubsub)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
            "Connection": "keep-alive",
        },
    )
```

### 5.3 `backend/app/services/mock_outbound_bus.py`

```python
import json
import asyncio
from typing import AsyncIterator
from app.core.redis_client import get_redis

_PREFIX = "mock:outbound:"

def publish(target_key: str, payload: dict) -> None:
    """出站拦截 → 发布到目标 channel。
    target_key: 点对点用 external_userid；群消息用 f'chat:{chat_id}'。
    """
    r = get_redis()
    r.publish(f"{_PREFIX}{target_key}", json.dumps(payload, ensure_ascii=False))

def subscribe(target_key: str):
    r = get_redis()
    pubsub = r.pubsub()
    pubsub.subscribe(f"{_PREFIX}{target_key}")
    return pubsub

async def iter_frames(pubsub) -> AsyncIterator[str]:
    """把 pubsub message 转成 SSE 帧；每 15s 一次 ping 保活。"""
    last_ping = asyncio.get_event_loop().time()
    while True:
        msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if msg and msg.get("type") == "message":
            data = msg["data"]
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            yield f"event: message\ndata: {data}\n\n"
        now = asyncio.get_event_loop().time()
        if now - last_ping >= 15:
            yield f"event: ping\ndata: {{\"ts\":{int(now)}}}\n\n"
            last_ping = now
        await asyncio.sleep(0.05)

async def unsubscribe(pubsub) -> None:
    try:
        pubsub.unsubscribe()
        pubsub.close()
    except Exception:
        pass
```

**注意**：redis-py 的 pubsub 是同步阻塞；高并发下 CPU 占用敏感。本实现先用 `get_message(timeout=1.0)` + `asyncio.sleep(0.05)` 轮询模式（简单、延迟 <1s）；如后续性能不够再迁 `aioredis`。

### 5.4 `backend/app/wecom/client.py` — 出站拦截

**真实调用路径 diff 必须为 0**，只在方法体入口加守卫：

```python
# send_text 方法开头加：
def send_text(self, touser: str, content: str) -> dict:
    payload = {
        "touser": touser,
        "msgtype": "text",
        "agentid": self.agentid,
        "text": {"content": content},
    }
    if settings.mock_wework:
        from app.services import mock_outbound_bus
        mock_outbound_bus.publish(touser, payload)
        return {"errcode": 0, "errmsg": "ok", "msgid": f"mock_{secrets.token_hex(8)}"}
    # ↓↓↓ 以下为原真实调用路径（不动一行）↓↓↓
    return self._http_post("/cgi-bin/message/send", json=payload)

# send_text_to_group 同理：
def send_text_to_group(self, chat_id: str, content: str) -> dict:
    payload = {
        "chatid": chat_id,
        "msgtype": "text",
        "text": {"content": content},
        "safe": 0,
    }
    if settings.mock_wework:
        from app.services import mock_outbound_bus
        mock_outbound_bus.publish(f"chat:{chat_id}", payload)
        return {"errcode": 0, "errmsg": "ok", "msgid": f"mock_{secrets.token_hex(8)}"}
    # ↓↓↓ 原真实路径 ↓↓↓
    return self._http_post("/cgi-bin/appchat/send", json=payload)
```

### 5.5 `backend/app/main.py` — 条件注册

```python
# 在现有 include_router 调用下方追加：
if settings.mock_wework:
    from app.api import mock_wework as mock_wework_router
    app.include_router(mock_wework_router.router)
    logger.warning(
        "MOCK_WEWORK enabled — /mock/wework/* routes registered. "
        "NEVER USE IN PRODUCTION."
    )
```

### 5.6 `backend/sql/seed_mock_users.sql`

```sql
-- 幂等 seed：模拟 UI 用户（external_userid 前缀 wm_mock_）
-- 运行：mysql -u <user> <db> < backend/sql/seed_mock_users.sql
-- 清理：DELETE FROM user WHERE external_userid LIKE 'wm_mock_%';

INSERT INTO user (
  external_userid, role, display_name, company, contact_person,
  can_search_jobs, can_search_workers, status
)
VALUES
  ('wm_mock_worker_001',  'worker',  '张工',       NULL,              '张工',   1, 0, 'active'),
  ('wm_mock_worker_002',  'worker',  '李师傅',     NULL,              '李师傅', 1, 0, 'active'),
  ('wm_mock_factory_001', 'factory', '华东电子厂', '华东电子有限公司', '王经理', 0, 1, 'active'),
  ('wm_mock_broker_001',  'broker',  '速聘中介',   '速聘人力资源',     '赵中介', 0, 1, 'active')
ON DUPLICATE KEY UPDATE display_name = VALUES(display_name);
```

### 5.7 `.env.example`

```bash
# ---- Mock 企业微信测试台（生产禁用）----
# 仅本地 / 预发环境可开启；ENV=production + MOCK_WEWORK=true 会拒绝启动
MOCK_WEWORK=false
```

## 6. 前端模块实现

### 6.1 路由

`frontend/src/router/index.js`：

```javascript
const routes = [
  // ... 既有路由
  { path: '/mock', component: () => import('@/views/mock/MockEntryView.vue'), meta: { skipAuth: true } },
  { path: '/mock/split', component: () => import('@/views/mock/MockSplitView.vue'), meta: { skipAuth: true } },
  { path: '/mock/single', component: () => import('@/views/mock/MockSingleView.vue'), meta: { skipAuth: true } },
]

router.beforeEach(async (to, from) => {
  if (to.meta?.skipAuth) return true      // ← 新增：直通
  // ... 既有鉴权逻辑
})
```

### 6.2 `MockBanner.vue`

```vue
<template>
  <div class="mock-banner">
    ⚠️ MOCK-WEWORK TESTBED — 非真实企业微信环境 ⚠️
  </div>
</template>
<style scoped>
.mock-banner {
  position: fixed; top: 0; left: 0; right: 0;
  background: #d32f2f; color: #fff;
  padding: 6px 16px; font-weight: bold; text-align: center;
  z-index: 9999; font-size: 13px;
}
</style>
```

**无 props、无 v-if、无条件渲染**，任何挂载位置都必须显示。

### 6.3 `MockIdentityPicker.vue`

```vue
<template>
  <el-select v-model="selected" @change="onChange" filterable placeholder="选择身份">
    <el-option-group v-for="group in grouped" :key="group.role" :label="group.label">
      <el-option
        v-for="u in group.users" :key="u.external_userid"
        :label="`${u.name}（${u.external_userid}）`"
        :value="u.external_userid" />
    </el-option-group>
  </el-select>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import { fetchMockUsers } from '@/api/mock'

const props = defineProps({ modelValue: String, roleFilter: { type: Array, default: null } })
const emit = defineEmits(['update:modelValue', 'change'])
const users = ref([])
const selected = ref(props.modelValue)

const grouped = computed(() => {
  const labels = { worker: '求职者', factory: '招聘者（厂家）', broker: '招聘者（中介）' }
  const buckets = { worker: [], factory: [], broker: [] }
  const filtered = props.roleFilter ? users.value.filter(u => props.roleFilter.includes(u.role)) : users.value
  for (const u of filtered) buckets[u.role]?.push(u)
  return Object.entries(buckets)
    .filter(([, us]) => us.length)
    .map(([role, us]) => ({ role, label: labels[role], users: us }))
})

onMounted(async () => { users.value = (await fetchMockUsers()).users || [] })
function onChange(v) { emit('update:modelValue', v); emit('change', v) }
</script>
```

### 6.4 `MockChatPanel.vue`

```vue
<template>
  <div class="mock-panel">
    <MockIdentityPicker v-model="externalUserid" :role-filter="props.roleFilter" @change="onIdentityChange" />
    <div class="messages">
      <div v-for="(m, i) in messages" :key="i" :class="['bubble', m.direction]">
        <div class="content">{{ m.content }}</div>
        <div class="meta">{{ m.direction === 'in' ? '我发送' : 'bot 回复' }} · {{ formatTs(m.ts) }}</div>
      </div>
    </div>
    <div class="composer">
      <el-input v-model="draft" @keyup.enter="send" placeholder="输入消息，回车发送" />
      <el-button type="primary" @click="send">发送</el-button>
    </div>
  </div>
</template>
<script setup>
import { ref, onBeforeUnmount } from 'vue'
import { mockInbound, openMockSse } from '@/api/mock'
import MockIdentityPicker from './MockIdentityPicker.vue'

const props = defineProps({ roleFilter: Array, initialUserid: String })
const externalUserid = ref(props.initialUserid || '')
const messages = ref([])
const draft = ref('')
let es = null

function onIdentityChange(newId) {
  if (es) { es.close(); es = null }
  messages.value = []
  if (!newId) return
  es = openMockSse(newId, {
    onMessage: (payload) => {
      messages.value.push({ direction: 'out', content: payload?.text?.content || '', ts: Date.now() })
    },
  })
}

async function send() {
  if (!externalUserid.value || !draft.value.trim()) return
  const content = draft.value
  await mockInbound({
    ToUserName:   'wwmock_corpid',
    FromUserName: externalUserid.value,
    CreateTime:   Math.floor(Date.now() / 1000),
    MsgType:      'text',
    Content:      content,
    MsgId:        `mock_msgid_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    AgentID:      '1000002',
  })
  messages.value.push({ direction: 'in', content, ts: Date.now() })
  draft.value = ''
}

onBeforeUnmount(() => { if (es) es.close() })
function formatTs(ts) { return new Date(ts).toLocaleTimeString() }
</script>
```

### 6.5 `MockSplitView.vue`

```vue
<template>
  <div class="split-root">
    <MockBanner />
    <div class="split-container">
      <div class="pane"><h3>招聘者视角</h3><MockChatPanel :role-filter="['factory', 'broker']" /></div>
      <div class="pane"><h3>求职者视角</h3><MockChatPanel :role-filter="['worker']" /></div>
    </div>
  </div>
</template>
<script setup>
import MockBanner from '@/components/mock/MockBanner.vue'
import MockChatPanel from '@/components/mock/MockChatPanel.vue'
</script>
<style scoped>
.split-container { display: flex; padding-top: 32px; height: calc(100vh - 32px); }
.pane { flex: 1; padding: 16px; border-right: 1px solid #eee; overflow: auto; }
.pane:last-child { border-right: none; }
</style>
```

### 6.6 `MockSingleView.vue`

```vue
<template>
  <div class="single-root">
    <MockBanner />
    <h3>{{ roleLabel }}视角</h3>
    <MockChatPanel :role-filter="roleFilter" :initial-userid="initialUserid" />
  </div>
</template>
<script setup>
import { useRoute } from 'vue-router'
import MockBanner from '@/components/mock/MockBanner.vue'
import MockChatPanel from '@/components/mock/MockChatPanel.vue'

const route = useRoute()
const initialUserid = route.query.external_userid || ''
const role = route.query.role || 'worker'
const roleLabel = { worker: '求职者', factory: '招聘者（厂家）', broker: '招聘者（中介）' }[role] || role
const roleFilter = role === 'worker' ? ['worker'] : ['factory', 'broker']
</script>
```

### 6.7 `MockEntryView.vue`

```vue
<template><component :is="target" /></template>
<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import MockSplitView from './MockSplitView.vue'
import MockSingleView from './MockSingleView.vue'
const route = useRoute()
const target = computed(() => route.query.external_userid ? MockSingleView : MockSplitView)
</script>
```

### 6.8 `frontend/src/api/mock.js`

```javascript
import request from './request'

export async function fetchMockUsers() {
  const r = await request.get('/mock/wework/users')
  return r.data
}

export async function mockInbound(payload) {
  const r = await request.post('/mock/wework/inbound', payload)
  return r.data
}

export function openMockSse(externalUserid, { onMessage } = {}) {
  const url = `/api/mock/wework/sse?external_userid=${encodeURIComponent(externalUserid)}`
  const es = new EventSource(url)
  es.addEventListener('message', (e) => {
    try { onMessage && onMessage(JSON.parse(e.data)) } catch (err) { console.warn('SSE parse error', err) }
  })
  es.addEventListener('ping', () => {})
  es.addEventListener('ready', () => {})
  es.addEventListener('error', (e) => console.warn('SSE error', e))
  return es
}
```

## 7. 联调要点

- 启动顺序：`.env` 设 `MOCK_WEWORK=true` + `ENV=development` → `docker compose up -d` → 前端 `npm run dev` 或 `npm run build` + nginx 挂载
- 后端日志必出现 `MOCK_WEWORK enabled — ...` WARNING
- 浏览器开 `/mock` → 必见红色横幅 + 双栏
- 身份切换器下拉必列 seed 的 `wm_mock_*` 用户（按 role 分组）
- 任一栏发送消息后 ≤ 2 秒 SSE 气泡弹出
- Worker 日志必看到消费 `queue:incoming`

## 8. 已知坑与规避

| 坑 | 规避 |
|---|---|
| **SSE 被 nginx 缓冲** | 后端 header `X-Accel-Buffering: no`；或 nginx 配置 `proxy_buffering off` |
| **redis-py pubsub 阻塞** | 用 `get_message(timeout=1.0)` + `asyncio.sleep(0.05)` 轮询；后续压测若 CPU 高再迁 `aioredis` |
| **EventSource 重连风暴** | 切换身份前 `es.close()`；onError 日志观察是否有叠加 |
| **mock 用户被统计为真实用户** | DAU / 报表 SQL 里加 `AND external_userid NOT LIKE 'wm_mock_%'` |
| **`User.extra is None`** | 取字段时 `(u.extra or {}).get(...)` 兜底 |
| **CORS**：开发期前端 `localhost:5173` → 后端 `localhost:8000` | Vite proxy 或 nginx 反代统一 |
| **CI 误带 flag**：production 镜像启动崩 | CI 强制 grep `.env*` 禁止 `MOCK_WEWORK=true` 出现在 production 分支 |

## 9. 交付物清单

- [ ] 后端新增 2 文件（`api/mock_wework.py` / `services/mock_outbound_bus.py`）
- [ ] 后端修改 3 文件（`config.py` / `main.py` / `wecom/client.py`）
- [ ] SQL 新增 1 文件（`backend/sql/seed_mock_users.sql`）
- [ ] 前端新增 8 文件（`views/mock/*.vue` × 3 + `components/mock/*.vue` × 3 + `api/mock.js` + router 修改）
- [ ] `.env.example` 追加 1 段
- [ ] 单测文件 `backend/tests/unit/test_mock_wework.py` / `test_mock_wework_contract.py`（详见测试文档）
