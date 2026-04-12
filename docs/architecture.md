# JobBridge 技术架构设计

> 本文档描述代码级的分层架构、模块职责、接口契约、数据流向和扩展点
> 与《招聘撮合平台方案设计》（`方案设计_v0.1.md`）配套维护，版本同步更新

---

## 一、架构原则

1. **分层解耦**：表现层（API）→ 应用层（Service）→ 领域层（Models/Schemas）→ 基础设施层（DB/Redis/LLM/WeCom/Storage）。上层依赖下层，下层不依赖上层。
2. **依赖倒置**：Service 依赖**抽象接口**（LLM provider、存储 provider），不依赖具体实现。切换供应商只改注册代码，不改业务逻辑。
3. **管道模式**：消息处理是一条链：收消息 → 识别用户 → 判断意图 → 分发处理器 → 格式化回复 → 发消息。每一步可独立测试和替换。
4. **配置驱动**：所有可能变化的（LLM 模型、存储后端、审核阈值、业务参数）全走 `system_config` 表或 `.env`，不硬编码。
5. **扩展点显式化**：新增 LLM 供应商 / 存储后端 / 消息类型 / 定时任务，各加一个文件即可，不改已有代码。

---

## 二、目录结构

前后端分离，各在独立目录下：
- `backend/` — Python FastAPI 后端
- `frontend/` — Vue 3 运营后台前端
- 根目录 — Docker 编排、nginx、文档等项目级配置

```
backend/app/
├── main.py                         # FastAPI 入口，路由注册，中间件，生命周期
├── config.py                       # pydantic-settings 集中配置（已有）
├── db.py                           # SQLAlchemy engine + session + Base（已有）
├── models.py                       # ORM 模型（10 张表，单文件）
│
├── schemas/                        # Pydantic 数据传输对象（DTO）
│   ├── __init__.py
│   ├── user.py                     # UserCreate / UserOut / UserQuery
│   ├── job.py                      # JobCreate / JobOut / JobQuery / JobUpdate
│   ├── resume.py                   # ResumeCreate / ResumeOut / ...
│   ├── conversation.py             # SessionState / CriteriaPatch / ChatMessage
│   ├── llm.py                      # ExtractResult / RerankResult / IntentResult
│   ├── audit.py                    # AuditAction / AuditLogOut
│   └── admin.py                    # LoginRequest / TokenResponse / ConfigItem / ReportData
│
├── api/                            # 表现层：HTTP 路由
│   ├── __init__.py
│   ├── deps.py                     # 共享依赖（get_db / get_current_admin / get_redis）
│   ├── webhook.py                  # POST /webhook/wecom  企微回调入口（唯一）
│   ├── events.py                   # POST /api/events/*   外部事件回传（小程序埋点等）
│   └── admin/                      # 运营后台 /admin/* 路由组
│       ├── __init__.py             # admin_router 汇总
│       ├── auth.py                 # POST /admin/login  GET /admin/me
│       ├── audit.py                # 审核工作台 CRUD + 通过/驳回
│       ├── accounts.py             # 厂家/中介/工人/黑名单
│       ├── jobs.py                 # 岗位 CRUD + 下架/延期
│       ├── resumes.py              # 简历 CRUD
│       ├── dicts.py                # 城市/工种/敏感词字典
│       ├── config.py               # 系统配置读写
│       ├── reports.py              # 数据看板指标
│       └── logs.py                 # 对话日志查询
│
├── services/                       # 应用层：业务逻辑编排
│   ├── __init__.py
│   ├── worker.py                   # ★ 消息队列消费者：BLPOP queue:incoming → 调 message_router
│   ├── message_router.py           # 消息总调度：用户识别 → 意图判断 → handler 分发 → 回复
│   ├── user_service.py             # 注册 / 封禁 / 角色查询 / 首次交互欢迎语
│   ├── intent_service.py           # 调 LLM 做意图分类 + 结构化抽取 + 必填字段追问
│   ├── upload_service.py           # 岗位/简历入库流程（抽取 → 审核 → 存储）
│   ├── search_service.py           # 三步漏斗（硬过滤 → 重排 → 权限过滤 → 格式化）
│   ├── conversation_service.py     # Redis 会话状态管理 / criteria merge / shown_items / 超时
│   ├── audit_service.py            # 内容审核（敏感词 + LLM 安全 + 人工队列）
│   ├── permission_service.py       # 按角色做字段级过滤
│   └── report_service.py           # 统计指标 / 每日报表
│
├── llm/                            # 基础设施：LLM 抽象层（§4.3）
│   ├── __init__.py                 # get_intent_extractor() / get_reranker() 工厂函数
│   ├── base.py                     # ABC: IntentExtractor / Reranker
│   ├── prompts.py                  # 所有 prompt 模板集中管理
│   └── providers/
│       ├── __init__.py
│       ├── qwen.py                 # QwenIntentExtractor / QwenReranker
│       └── doubao.py               # DoubaoIntentExtractor / DoubaoReranker
│
├── wecom/                          # 基础设施：企微集成
│   ├── __init__.py
│   ├── crypto.py                   # AES 解密 / 签名校验
│   ├── callback.py                 # 解析回调 XML → 结构化 Message 对象
│   └── client.py                   # WeComClient: send_text / download_media / get_user_info
│
├── storage/                        # 基础设施：对象存储抽象
│   ├── __init__.py                 # get_storage() 工厂函数
│   ├── base.py                     # ABC: StorageBackend
│   └── local.py                    # LocalStorage（一期：本地文件系统）
│
├── tasks/                          # 定时任务
│   ├── __init__.py
│   ├── scheduler.py                # APScheduler 初始化 + 任务注册
│   ├── ttl_cleanup.py              # TTL 过期软删 + 定期硬删
│   └── daily_report.py             # 每日企微群推送日报
│
└── core/                           # 通用工具
    ├── __init__.py
    ├── exceptions.py               # 自定义业务异常
    ├── pagination.py               # 通用分页工具
    └── redis_client.py             # Redis 客户端封装
```

---

## 三、分层依赖关系

```
企微消息链路（异步）：
api/webhook.py ──→ core/redis_client.py（幂等检查 + RPUSH queue:incoming + 快速返回 200）
                       ↓
services/worker.py ──→ BLPOP queue:incoming
                       ↓
services/message_router.py ──→ services/intent_service.py ──→ llm/base.py
                             ├→ services/upload_service.py ──→ llm/base.py, storage/base.py
                             ├→ services/search_service.py ──→ llm/base.py
                             ├→ services/conversation_service.py ──→ core/redis_client.py
                             └→ services/user_service.py ──→ models.py
                                  ↓
wecom/client.py ──→ 异步回复用户

运营后台链路（同步）：
api/admin/*.py ──→ services/*_service.py ──→ models.py, core/*

事件回传链路（同步）：
api/events.py ──→ models.py（写入点击事件表）

所有 service ──→ models.py (ORM) + schemas/ (DTO)
所有 service ──→ 不直接依赖 llm/providers/ 或 storage/local.py，只依赖 llm/base.py 和 storage/base.py
```

**规则**：
- `api/webhook.py` **不直接调** `message_router`，只做验签 + 幂等 + 入队 + 快速返回
- `services/worker.py` 是消息队列的消费者，由它调用 `message_router.process()`
- `api/admin/*.py` 同步调 `services/`
- `services/` 通过工厂函数获取具体实现（`get_intent_extractor()` / `get_storage()`）
- `llm/providers/`、`storage/local.py` 只被工厂函数引用，业务代码无感知

---

## 四、核心接口契约

### 4.1 LLM 抽象层

```python
# llm/base.py
class IntentResult(BaseModel):
    intent: str            # upload_job / search_job / upload_and_search / follow_up / show_more / chitchat / command
    structured_data: dict  # 抽取出的结构化字段
    criteria_patch: list[dict]  # 多轮 criteria 增量: [{"op":"add|update|remove","field":"...","value":"..."}]
    missing_fields: list[str]
    confidence: float      # 0-1
    raw_response: str      # LLM 原始输出（调试用）

class IntentExtractor(ABC):
    @abstractmethod
    def extract(self, text: str, role: str, history: list[dict],
                current_criteria: dict | None) -> IntentResult: ...

class RerankResult(BaseModel):
    ranked_items: list[dict]
    reply_text: str
    raw_response: str

class Reranker(ABC):
    @abstractmethod
    def rerank(self, query: str, candidates: list[dict],
               role: str, top_n: int) -> RerankResult: ...
```

### 4.2 存储抽象层

```python
# storage/base.py
class StorageBackend(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes, content_type: str) -> str: ...
    @abstractmethod
    def get_url(self, key: str) -> str: ...
    @abstractmethod
    def delete(self, key: str) -> bool: ...
```

### 4.3 企微客户端

```python
# wecom/client.py
class WeComClient:
    def send_text(self, external_userid: str, content: str) -> bool: ...
    def download_media(self, media_id: str) -> tuple[bytes, str]: ...
    def get_external_contact(self, external_userid: str) -> dict | None: ...
```

### 4.4 会话状态契约（Redis Session）

权威定义在方案设计 §11.8，此处同步代码级契约。

**Redis Key**：`session:{external_userid}`，TTL = 1800 秒（30 分钟）

```python
# schemas/conversation.py
class CandidateSnapshot(BaseModel):
    candidate_ids: list[str]       # Reranker 排序后的完整候选 ID 列表
    ranking_version: int           # 每次重新检索 +1
    query_digest: str              # search_criteria 的 SHA256 前 12 位
    created_at: str                # ISO 8601
    expires_at: str                # created_at + 30 min

class SessionState(BaseModel):
    role: str                      # worker / factory / broker
    current_intent: str | None     # 当前意图
    search_criteria: dict          # 跨轮次累积 merge 的检索条件
    candidate_snapshot: CandidateSnapshot | None  # 检索快照（show_more 用）
    shown_items: list[str]         # 已展示的 ID 集合
    history: list[dict]            # 最近 6 轮 [{"role":"user","content":"..."}, ...]
    updated_at: str                # ISO 8601

class CriteriaPatch(BaseModel):
    op: str                        # add / update / remove
    field: str
    value: str | int | bool | None
```

**关键语义**：
- `show_more`：从 `candidate_snapshot.candidate_ids` 减去 `shown_items` 取下一批 3 条
- `criteria` 变更（追问修正）：`query_digest` 改变 → 丢弃 `candidate_snapshot` → 重新检索
- session 过期 / `/重新找`：整个 `SessionState` 清空

### 4.5 Prompt 设计规范（`llm/prompts.py`）

**结构约束**：
- `IntentExtractor` 的 prompt **必须**要求 LLM 输出**严格 JSON**（不允许 markdown 代码块包裹），字段与 `IntentResult` 一一对应
- `Reranker` 的 prompt 输出同理，对应 `RerankResult`
- 所有 prompt 必须包含 **2-3 个 few-shot 示例**，覆盖"正常输入"和"边界输入"（纯闲聊、空内容、超长文本）

**Token 预算**：
- IntentExtractor：input < 2000 tokens，output < 500 tokens（使用便宜模型，如 Qwen-turbo）
- Reranker：input < 4000 tokens（含候选集），output < 1000 tokens（含回复文本，使用质量模型如 Qwen-plus）

**错误兜底**：
- LLM 返回内容 JSON 解析失败 → 记录 `raw_response` 到日志 → fallback 到 `intent=chitchat`，回复"没太理解您的意思，请再具体描述一下需求"
- LLM 超时（`llm_timeout_seconds`）→ 最多重试 1 次 → 仍失败则回复"系统繁忙"
- LLM 返回的 intent 不在已知列表中 → 当作 `chitchat` 处理

**版本管理**：
- prompt 模板统一在 `prompts.py` 中定义为常量字符串
- 每个 prompt 带一个版本注释（如 `# v1.0 2026-04-12`），修改时更新版本号
- `conversation_log.criteria_snapshot` 中记录当时使用的 prompt 版本，方便效果回溯

**ORM 映射指导**（`models.py` 实现时注意）：
- ENUM 字段使用 Python 字符串类型 + `sa.Enum` 约束，不用 Python Enum 类（简化序列化）
- JSON 字段使用 `sa.JSON`，返回 Python dict/list
- `extra` 字段使用 `MutableDict.as_mutable(sa.JSON)`（SQLAlchemy 变更跟踪，避免修改后不触发 commit）
- 软删除字段 `deleted_at`：在 Service 层统一 `.filter(Model.deleted_at.is_(None))`，不在 ORM 层做全局过滤

---

## 五、消息处理数据流

```
[企微 POST /webhook/wecom]
    │
    ▼
api/webhook.py: 验签 → 解密 → MsgId 幂等检查 → 入 Redis 队列 → 立即返回 200（< 100ms）
    │
    ▼
services/worker.py: BLPOP queue:incoming → 取出消息
    │
    ▼
services/message_router.py:
    ├── user_service.identify_or_register(userid)
    │     ├── 新工人 → 自动注册 → 发欢迎语 → return
    │     ├── 新厂家/中介（首次） → 发个性化欢迎语 → continue
    │     └── 已封禁 → 发封禁通知 → return
    │
    ├── 消息类型判断
    │     ├── 图片 → storage.save() + 存 media key
    │     ├── 语音/文件 → 回复"暂不支持" → return
    │     └── 文本 → continue
    │
    ├── intent_service.classify(text, role, history, criteria)
    │     └── → IntentResult { intent, structured_data, missing_fields }
    │
    ├── 按 intent 分发:
    │     ├── upload_job / upload_resume
    │     │     → upload_service.process()
    │     │         ├── 必填检查 → 缺失则追问 → return 追问消息
    │     │         ├── audit_service.check() → 审核
    │     │         └── 入库 → return "已入库"
    │     │
    │     ├── upload_and_search (厂家"发布岗位 + 顺便找工人")
    │     │     → upload_service.process() (先入库)
    │     │     → search_service.search() (再检索匹配工人)
    │     │     → return "岗位已入库，同时为您找到 N 位匹配工人：..."
    │     │
    │     ├── search_job / search_worker / follow_up
    │     │     → conversation_service.merge_criteria()
    │     │     → search_service.search()
    │     │         ├── MySQL 硬过滤
    │     │         ├── Reranker 重排
    │     │         ├── permission_service.filter_fields()
    │     │         └── 格式化回复文本
    │     │     → conversation_service.record_shown()
    │     │     → return 推荐回复
    │     │
    │     ├── show_more → conversation_service.get_next_batch()
    │     ├── command → 对应处理
    │     └── chitchat → return 引导语
    │
    ▼
wecom/client.py: send_text(userid, reply)
    │
    ▼
conversation_log: 记录 in/out 消息
```

---

## 六、扩展点清单

| 扩展场景 | 操作 | 不用改的 |
|---|---|---|
| 新增 LLM 供应商 | `llm/providers/` 加一个文件 + `__init__.py` 注册 | services/ / api/ |
| 新增存储后端 | `storage/` 加一个文件 + `__init__.py` 注册 | services/ / api/ |
| 新增消息类型 | `message_router.py` 加分支 | 其他 service |
| 新增意图类型 | `intent_service.py` + `prompts.py` | 已有意图 |
| 新增定时任务 | `tasks/` 加文件 + `scheduler.py` 注册 | 其他任务 |
| 新增 admin 页面 | `api/admin/` 加 router + service | 已有页面 |
| 新增硬过滤字段 | `models.py` + Alembic + `search_service.py` | LLM / admin |
| 二期加向量检索 | `search_service.py` 中间插 embedding 步骤 | 硬过滤和 rerank |
| 二期加 RBAC | `api/deps.py` 替换鉴权 | services/ |

---

## 七、前端架构

### 7.1 技术栈与项目规范

| 项 | 选型 | 备注 |
|---|---|---|
| 框架 | **Vue 3**（Composition API + `<script setup>`） | |
| UI 组件库 | **Element Plus** | 中文后台最成熟生态 |
| 构建工具 | **Vite 5+** | |
| 状态管理 | **Pinia** | 轻量，Vue 3 官方推荐 |
| 路由 | **Vue Router 4** | |
| HTTP 客户端 | **Axios** | 统一封装 request/response 拦截器 |
| 图表 | **ECharts 5**（通过 `vue-echarts`） | 数据看板用 |
| 代码规范 | **ESLint + Prettier** | 提交前自动格式化 |
| TypeScript | **推荐但不强制** | 如果前端开发者熟悉 TS 则用，否则纯 JS 也行 |

### 7.2 前端目录结构（建议）

```
frontend/
├── prototype/                # 原型 Demo（开发参考，不进生产构建）
│   └── index.html
├── public/
│   └── favicon.ico
├── src/
│   ├── main.js               # 入口
│   ├── App.vue
│   ├── router/
│   │   └── index.js          # 路由配置（对齐 §13.4 的 15 个页面路径）
│   ├── stores/               # Pinia 状态
│   │   ├── auth.js           # 登录态 / JWT token
│   │   └── app.js            # 全局状态（侧边栏收起、通知列表等）
│   ├── api/                  # 后端 API 调用封装（每个模块一个文件）
│   │   ├── request.js        # Axios 实例 + 拦截器
│   │   ├── auth.js           # login / me
│   │   ├── audit.js          # 审核工作台
│   │   ├── accounts.js       # 账号管理
│   │   ├── jobs.js           # 岗位管理
│   │   ├── resumes.js        # 简历管理
│   │   ├── dicts.js          # 字典管理
│   │   ├── config.js         # 系统配置
│   │   ├── reports.js        # 数据看板
│   │   └── logs.js           # 对话日志
│   ├── views/                # 页面组件（对齐路由，每页一个 .vue）
│   │   ├── login/
│   │   ├── dashboard/
│   │   ├── audit/
│   │   ├── accounts/
│   │   ├── jobs/
│   │   ├── resumes/
│   │   ├── dicts/
│   │   ├── config/
│   │   ├── reports/
│   │   └── logs/
│   ├── components/           # 通用组件
│   │   ├── layout/           # 侧边菜单 + 顶栏 + 主内容区
│   │   ├── PageTable.vue     # 通用表格（分页 + 筛选 + 排序 + 导出）
│   │   ├── DetailDrawer.vue  # 通用详情抽屉
│   │   └── ImagePreview.vue  # 图片预览 Modal
│   ├── composables/          # 可复用的 Composition API hooks
│   │   ├── usePageTable.js   # 表格分页逻辑
│   │   └── useKeyboard.js    # 审核工作台键盘快捷键
│   └── utils/
│       ├── constants.js      # 枚举值 / 字典映射
│       └── format.js         # 日期格式化 / 脱敏显示
├── index.html
├── vite.config.js
├── package.json
└── .eslintrc.js
```

### 7.3 前后端通信契约

#### 7.3.1 基础约定

| 项 | 规范 |
|---|---|
| 基础路径 | 所有 admin API 在 `/admin/*` 下 |
| 协议 | HTTPS（生产）/ HTTP（开发） |
| 数据格式 | `Content-Type: application/json`，UTF-8 |
| 鉴权方式 | JWT Bearer Token：`Authorization: Bearer <token>` |
| Token 获取 | `POST /admin/login` → 返回 `{ token, expires_at }` |
| Token 过期 | 24 小时，过期后前端跳转登录页 |

#### 7.3.2 统一响应格式

**成功响应**（HTTP 200）：
```json
{
  "code": 0,
  "message": "ok",
  "data": { ... }
}
```

**分页列表响应**：
```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [ ... ],
    "total": 127,
    "page": 1,
    "size": 20,
    "pages": 7
  }
}
```

**错误响应**（HTTP 4xx/5xx）：
```json
{
  "code": 40001,
  "message": "用户名或密码错误",
  "data": null
}
```

#### 7.3.3 错误码约定

| 范围 | 含义 | 前端处理 |
|---|---|---|
| `0` | 成功 | 正常渲染 |
| `40001-40099` | 鉴权错误（token 过期 / 无效 / 密码错） | 跳转登录页 |
| `40100-40199` | 参数校验失败 | 表单字段高亮提示 |
| `40300-40399` | 权限不足 | Toast 提示 |
| `40400-40499` | 资源不存在 | Toast 或空状态 |
| `50000-50099` | 服务端内部错误 | Toast "系统繁忙" |
| `50100-50199` | LLM 服务异常 | Toast "AI 服务暂时不可用" |

#### 7.3.4 Axios 拦截器要点（`frontend/src/api/request.js`）

**请求拦截器**：
- 从 Pinia store 读取 JWT token，自动带 `Authorization` header
- 如果 token 为空且不是 `/admin/login` 请求，直接跳登录页

**响应拦截器**：
- `code === 0`：返回 `response.data.data`
- `code === 40001`（token 过期）：清除本地 token → 跳登录页
- 其它错误：`ElMessage.error(response.data.message)` 统一 Toast

### 7.4 前端需要对接的 Admin API 清单

按页面分组，每组列出后端路由：

| 页面 | 后端 API | 方法 | 说明 |
|---|---|---|---|
| **登录** | `/admin/login` | POST | `{username, password}` → `{token, expires_at}` |
| | `/admin/me` | GET | 当前登录用户信息 |
| | `/admin/me/password` | PUT | 修改密码 `{old_password, new_password}`；首次登录（`password_changed=false`）时强制跳转 |
| **Dashboard** | `/admin/reports/dashboard` | GET | 今日核心指标 + 趋势数据 |
| | `/admin/audit/pending-count` | GET | 待审数量（侧边栏 badge） |
| **审核工作台** | `/admin/audit/queue` | GET | 待审队列（分页 + 筛选）；每条返回 `locked_by` 字段（null 或其他审核员 ID） |
| | `/admin/audit/{id}` | GET | 单条详情（含 LLM 抽取 + 审核建议 + `version` 字段） |
| | `/admin/audit/{id}/lock` | POST | 打开条目时自动调用，软锁 300 秒（`SETNX audit_lock:{id}`），防止并发冲突 |
| | `/admin/audit/{id}/unlock` | POST | 离开 / 切换下一条时释放锁 |
| | `/admin/audit/{id}/pass` | POST | 通过。请求必须带 `version` 字段做乐观锁校验 |
| | `/admin/audit/{id}/reject` | POST | 驳回（`{reason, notify, block_user, version}`） |
| | `/admin/audit/{id}/edit` | PUT | 修正字段（带 `version`） |
| | `/admin/audit/{id}/undo` | POST | 撤销上一个动作（仅在 **30 秒内** 有效，后端通过 Redis 暂存 `undo_action:{id}` 实现） |
| **厂家管理** | `/admin/accounts/factories` | GET | 列表（分页 + 筛选） |
| | `/admin/accounts/factories` | POST | 预注册 |
| | `/admin/accounts/factories/{id}` | GET/PUT | 详情 / 编辑 |
| | `/admin/accounts/factories/import` | POST | Excel 批量导入 |
| **中介管理** | `/admin/accounts/brokers` | 同厂家，多 `can_search_jobs/workers` 字段 |
| **工人管理** | `/admin/accounts/workers` | GET | 列表（只读） |
| **黑名单** | `/admin/accounts/blacklist` | GET | 列表 |
| | `/admin/accounts/{userid}/block` | POST | 封禁 `{reason}` |
| | `/admin/accounts/{userid}/unblock` | POST | 解封 `{reason}` |
| **岗位管理** | `/admin/jobs` | GET | 列表（高级筛选 + 分页） |
| | `/admin/jobs/{id}` | GET/PUT | 详情 / 编辑字段 |
| | `/admin/jobs/{id}/delist` | POST | 下架，请求体 `{reason: "manual_delist"\|"filled"}`，对应 `delist_reason` 枚举 |
| | `/admin/jobs/{id}/extend` | POST | 延期 `{days: 15\|30}` |
| **简历管理** | `/admin/resumes` | 同岗位 |
| **字典 · 城市** | `/admin/dicts/cities` | GET/PUT | 列表 / 编辑别名 |
| **字典 · 工种** | `/admin/dicts/job-categories` | GET/POST/PUT/DELETE | CRUD |
| **字典 · 敏感词** | `/admin/dicts/sensitive-words` | GET/POST/DELETE | CRUD + 批量导入 |
| | `/admin/dicts/sensitive-words/batch` | POST | 批量添加 |
| **系统配置** | `/admin/config` | GET | 所有配置项（分组） |
| | `/admin/config/{key}` | PUT | 更新单项 `{value}` |
| **数据看板** | `/admin/reports/trends` | GET | 趋势数据 `?range=7d\|30d\|custom` |
| | `/admin/reports/top` | GET | TOP 榜单 |
| | `/admin/reports/funnel` | GET | 转化漏斗 |
| | `/admin/reports/export` | GET | 导出 CSV |
| **对话日志** | `/admin/logs/conversations` | GET | `?userid=xxx&start=&end=` |
| | `/admin/logs/conversations/export` | GET | 导出 CSV |
| **事件回传** | `/api/events/miniprogram_click` | POST | 小程序详情页点击埋点回传 `{userid, job_id\|resume_id, timestamp}`；幂等：同一 userid + 同一 target_id 10 分钟内去重；鉴权：简单 API Key（`.env` 配置，不走 JWT）；写入 `event_log` 表供 §17.1 "详情点击率"统计 |

> 后端 API 文档在开发环境自动生成：`http://localhost:8000/docs`（Swagger UI），前端开发者可以直接在浏览器里试调。

### 7.5 本地开发联调

前端 Vite dev server 跑在 `5173` 端口，后端 uvicorn 跑在 `8000` 端口。通过 Vite 代理解决跨域：

```js
// frontend/vite.config.js
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      '/admin': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
```

**联调启动步骤**：
```bash
# 终端 1：启动基础设施
docker compose up -d

# 终端 2：启动后端
cd backend && source .venv/Scripts/activate && uvicorn app.main:app --reload

# 终端 3：启动前端
cd frontend && npm run dev

# 浏览器打开 http://localhost:5173/admin/login
```

### 7.6 前端开发规范

1. **路由路径必须与 §13.4 一致**（`/admin/login`、`/admin/dashboard`、`/admin/audit` 等），不要自己改路径
2. **API 调用统一走 `src/api/` 封装**，不要在 view 里直接写 axios.get
3. **分页参数统一用 `page` + `size`**，与后端 `PageParams` 对齐
4. **表格组件尽量复用 `PageTable.vue`**，避免每页重写分页/排序逻辑
5. **审核工作台的键盘快捷键**逻辑放在 `composables/useKeyboard.js`，与 UI 解耦
6. **所有列表页必须支持**：分页 / 排序 / 筛选 / 导出 CSV
7. **所有编辑操作必须有**：脏数据保护（离开前提醒）/ 保存 loading / 成功 Toast
8. **所有危险操作必须有**：二次确认弹窗
9. **深色模式**：使用 Element Plus 的 `dark` CSS 变量方案，不要硬写颜色
10. **错误处理统一在 Axios 拦截器**，view 里不需要重复 try/catch

---

## 八、实施顺序

| Phase | 内容 | 验证 |
|---|---|---|
| 1 | models.py + schemas/ + core/ | `from app.models import *` 成功 |
| 2 | llm/ + storage/ | 单测抽取一条岗位信息 |
| 3 | services/ (业务核心) | 脚本模拟完整检索流程 |
| 4 | wecom/ + webhook + message_router | 企微发消息收到回复 |
| 5 | api/admin/ | Swagger UI 操作全部 CRUD |
| 6 | tasks/ + 端到端联调 | Docker 全流程跑通 |
