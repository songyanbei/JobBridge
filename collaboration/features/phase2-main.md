# Feature: Phase 2 基础设施适配与契约骨架
> 状态：`done`
> 创建日期：2026-04-12
> 完成日期：2026-04-12
> 对应实施阶段：Phase 2
> 关联实施文档：`docs/implementation-plan.md` §4.3
> 关联方案设计章节：§4.2、§4.3、§10、§11.8、§12、§14.1.1、§14.2
> 关联架构章节：§一、§三、§4.1、§4.2、§4.3、§4.4、§4.5
> 配套文档：
> - 开发实施文档：`collaboration/features/phase2-dev-implementation.md`
> - 开发 Checklist：`collaboration/features/phase2-dev-checklist.md`
> - 测试实施文档：`collaboration/features/phase2-test-implementation.md`
> - 测试 Checklist：`collaboration/features/phase2-test-checklist.md`

## 1. 阶段目标

Phase 2 的目标不是做业务闭环，而是把后续主链路依赖的外部设施封装稳定下来，做到：

- 业务层未来只依赖抽象接口，不依赖具体 LLM、企微、存储厂商
- Prompt 先形成可扩展骨架和严格 JSON 契约，不在本阶段追求最终抽取效果
- 企微入站/出站、对象存储、LLM provider 都具备最小可测封装
- Phase 4 会直接依赖的消息基础契约先被锁定，而不是等到 webhook/worker 开发时临时决定
- 这些基础设施模块都可以被 mock，便于 Phase 3 service 层单测和 Phase 4 主链路联调

Phase 2 完成后，项目至少要具备以下能力：

- 可通过统一工厂切换 `IntentExtractor` / `Reranker` provider
- 可通过统一工厂切换对象存储后端，一期默认本地文件系统
- 企微回调消息可完成验签、解密、XML 解析并转换成统一消息对象
- 企微基础客户端可封装发消息、拉取素材、查联系人等基础 HTTP 调用
- prompt 已有统一入口、版本标记、token 预算、严格 JSON 约束和兜底约束
- Redis 限流、幂等、入队、死信、`wecom_inbound_event` 状态流转契约已文档化

## 2. 当前代码现状

当前仓库中，Phase 1 已交付的基础部分包括：

- `backend/app/config.py`：已具备 MySQL、Redis、LLM、企微、存储等基础配置项
- `backend/app/db.py`、`backend/app/models.py`、`backend/app/schemas/*`：数据层已完成
- `backend/app/core/redis_client.py`：session、幂等、限流、队列、锁方法已可复用
- `backend/app/main.py`：应用和 `/health` 已可启动

当前与 Phase 2 直接相关的代码状态：

- `backend/app/llm/base.py` 已存在，包含 `IntentExtractor`、`Reranker`、`IntentResult`、`RerankResult`
- `backend/app/storage/base.py` 已存在，包含 `StorageBackend`
- `backend/app/llm/providers/` 目录当前只有 `__init__.py`
- `backend/app/wecom/` 目录当前只有 `__init__.py`
- `backend/app/storage/` 当前没有 `local.py`
- `backend/app/llm/prompts.py` 当前不存在
- `backend/app/llm/__init__.py`、`backend/app/storage/__init__.py` 当前未形成可用工厂逻辑
- 当前还没有 Phase 2 对应的自动化测试

另外，虽然 `redis_client.py` 与 `wecom_inbound_event` 已在 Phase 1 交付，但目前还缺：

- 对 `check_rate_limit()`、`check_msg_duplicate()`、`enqueue_message()`、`dequeue_message()` 的调用顺序约定
- 对 `queue:incoming`、`queue:dead_letter` 的命名与用途约定
- 对 `wecom_inbound_event` 状态流转的统一文档

结论：Phase 2 目前仍处于“抽象接口有了，但具体 provider、工厂、基础封装、消息契约和测试基本空白”的状态。

## 3. 本阶段范围

### 3.1 本阶段必须完成

1. LLM 抽象适配层
- 完成 `llm/prompts.py`
- 完成 `llm/providers/qwen.py`
- 完成 `llm/providers/doubao.py`
- 完成 `llm/__init__.py` 中的 provider 工厂注册逻辑
- 明确超时、重试、解析失败兜底、`raw_response` 保留策略

2. Prompt 骨架
- 统一管理 prompt 模板常量
- 固定严格 JSON 输出约束
- 固定 prompt 版本注释规则
- 固定 input/output token 预算上限
- 固定基础错误兜底出口
- 给出结构示例或最小 few-shot 骨架，用于验证输出契约

3. 对象存储基础封装
- 完成 `storage/local.py`
- 完成 `storage/__init__.py` 中的工厂逻辑
- 支持本地文件保存、URL 生成、删除、存在性检查
- 锁定统一 key 命名规则

4. 企业微信基础封装
- 完成 `wecom/crypto.py`
- 完成 `wecom/callback.py`
- 完成 `wecom/client.py`
- 支持验签、消息解密、XML 解析、统一消息对象转换、消息发送 API 封装

5. 消息基础契约确认
- 文档化 `check_rate_limit()`、`check_msg_duplicate()`、`enqueue_message()`、`dequeue_message()` 的调用约定
- 明确 `queue:incoming`、`queue:dead_letter` 的用途和命名约定
- 明确 `wecom_inbound_event` 的状态流转契约
- 明确失败重试上限和进入死信队列的条件

6. 自动化测试
- 为 LLM provider、prompt 骨架、存储、企微基础封装补齐单元/集成测试
- 所有基础设施模块都必须支持 mock 测试

### 3.2 本阶段明确不做

- 不实现 `services/*.py` 业务编排
- 不实现 `api/webhook.py`
- 不实现 `services/worker.py`
- 不实现完整消息路由或意图分发
- 不定稿业务 prompt 内容和 few-shot 效果
- 不验证真实 LLM 效果指标
- 不接入真实企微联调环境作为通过前提
- 不实现 admin API
- 不实现前端页面或前后端联调

## 4. 真值来源与实现基线

出现文档不一致时，按以下优先级执行：

1. `docs/implementation-plan.md` 中 Phase 2 定义
2. `docs/architecture.md` 中目录结构、接口契约、Prompt 规范
3. `方案设计_v0.1.md` 中 §4.2、§4.3、§10、§11.8、§12 的约束
4. 本文档

特别说明：

- Prompt 最终效果和业务 few-shot 定稿属于 Phase 3，不以 Phase 2 产出效果作为验收依据
- 但 Prompt 的结构、版本、token 预算、JSON 契约、超时/错误兜底必须在 Phase 2 先固定
- Redis 队列、锁、session 基础方法已在 Phase 1 提供，Phase 2 原则上复用，不重复发明一套队列封装
- Phase 2 虽不实现 webhook / worker，但必须把 Phase 4 会直接依赖的消息契约先文档化并锁定
- `doubao.py` 在 Phase 2 以“结构骨架 + mock 测试可过”为交付标准，不以真实 API 联调成功作为阻塞条件

## 5. 详细需求说明

### 5.1 LLM 抽象层

涉及文件：

- `backend/app/llm/base.py`
- `backend/app/llm/prompts.py`
- `backend/app/llm/__init__.py`
- `backend/app/llm/providers/qwen.py`
- `backend/app/llm/providers/doubao.py`

要求：

- 业务层未来只能依赖 `IntentExtractor`、`Reranker` 抽象，不允许直接 import 具体 provider 类
- `llm/__init__.py` 必须暴露统一工厂，例如：
  - `get_intent_extractor(provider: str | None = None)`
  - `get_reranker(provider: str | None = None)`
- 当未显式传入 provider 时，默认读取 `settings.llm_provider`
- 对未知 provider 必须抛出明确异常，不能静默 fallback
- provider 实现使用 `httpx` 同步模式，符合方案设计已锁定技术栈
- provider 层负责：
  - 构造请求
  - 调用外部 REST API
  - 处理超时
  - 最多一次重试
  - 解析响应
  - 把结果转换为 `IntentResult` / `RerankResult`
- 若 LLM 返回非法 JSON：
  - 保留 `raw_response`
  - 走统一兜底
  - `IntentExtractor` fallback 为 `intent="chitchat"`、`confidence=0.0`
- 若 LLM 超时且重试后仍失败：
  - 返回统一失败结果或抛出统一异常
  - 行为必须可被测试稳定验证
- 对未知 intent 值，provider 层按 `chitchat` 处理并保留 `raw_response`
- Provider 层只负责返回统一 fallback 结果或抛出统一异常，不负责最终面向用户的回复文案
- “没太理解您的意思，请再具体描述一下需求”“系统繁忙”等用户回复文案属于 Phase 3 service 层职责
- timeout 重试上限在 provider 层固定为 1 次，不留给调用方自行决定

### 5.2 Prompt 骨架

涉及文件：

- `backend/app/llm/prompts.py`

要求：

- 所有 prompt 模板集中放在 `prompts.py`，不能把 prompt 散落在 provider 文件中
- 每个 prompt 必须包含版本注释，例如 `# v1.0 2026-04-12`
- 每个 prompt 必须在顶部注释中标注 input/output token 预算上限
- 必须有明确的“严格 JSON 输出，不允许 markdown code fence”约束
- Prompt 内容必须与 `IntentResult` / `RerankResult` 字段契约一致
- 必须保留 `raw_response` 相关的处理前提，便于后续落日志
- 必须体现基础错误兜底规则：
  - 解析失败 fallback
  - intent 非法值 fallback
  - 超时 fallback
- 本阶段可以先写“结构型 few-shot”或最小示例，重点是验证 JSON 契约和字段对齐
- 本阶段不要求把业务话术、追问策略、边界输入表现调到最优

Token 预算基线：

- `IntentExtractor`：input `< 2000 tokens`，output `< 500 tokens`
- `Reranker`：input `< 4000 tokens`，output `< 1000 tokens`

### 5.3 对象存储抽象层

涉及文件：

- `backend/app/storage/base.py`
- `backend/app/storage/local.py`
- `backend/app/storage/__init__.py`

要求：

- `storage/__init__.py` 必须提供统一工厂，例如 `get_storage(provider: str | None = None)`
- 一期默认后端为本地文件系统
- `LocalStorage` 必须实现 `StorageBackend` 四个基础方法：
  - `save`
  - `get_url`
  - `delete`
  - `exists`
- 本阶段统一 key 模板约定为 `{entity_type}/{record_id}/{uuid}.{ext}`
- 第一层使用实体类型，例如 `jobs` / `resumes` / `avatars`
- 第二层使用记录 ID，而不是用户自由输入
- 文件名冲突不允许覆盖原文件，统一使用 UUID 或等价唯一值规避冲突
- `save` 必须自动创建中间目录
- 返回值必须稳定，可供后续 service 和前端详情页复用
- 文件路径规则必须可测试，不能依赖人工判断
- 如现有配置不足以支撑本地目录配置，允许在本阶段新增最小配置项，但不得引入二期存储能力

### 5.4 企业微信基础封装

涉及文件：

- `backend/app/wecom/crypto.py`
- `backend/app/wecom/callback.py`
- `backend/app/wecom/client.py`

要求：

- `crypto.py` 负责：
  - 签名校验
  - 回调消息解密
  - 必要的内部 helper
- `callback.py` 负责：
  - 解析企微回调 XML
  - 转换为统一消息对象
  - 至少覆盖一期范围内的文本和图片消息
- 统一消息对象必须至少包含：
  - `msg_id`
  - `from_user`
  - `to_user`
  - `msg_type`
  - `content`
  - `media_id`
  - `create_time`
- `client.py` 负责：
  - 封装 access token 获取
  - 封装 `send_text`
  - 封装 `download_media`
  - 封装 `get_external_contact`
- `client.py` 必须把 HTTP 细节藏在内部，未来 service 只能调用方法，不允许手拼 URL
- Phase 2 不要求接上真实企微环境，但必须做到接口契约清晰、可 mock、可单测
- `get_external_contact()` 返回 `None` 只表示“用户不存在或已被删除”
- 网络错误、鉴权失败、接口响应异常等情况应抛异常，不允许返回 `None` 混淆语义

### 5.5 消息基础契约

虽然 webhook 和 worker 属于 Phase 4，但 Phase 2 必须先把基础契约锁定，供后续直接复用。

Redis 调用约定：

- webhook 入口先调用 `check_rate_limit()`，被限流消息直接返回，不入队
- 通过限流后再调用 `check_msg_duplicate()`，重复消息不重复入队
- 非重复消息调用 `enqueue_message("queue:incoming", payload)`
- worker 通过 `dequeue_message("queue:incoming")` 消费消息
- 达到重试上限后的消息转入 `enqueue_message("queue:dead_letter", payload)`

`wecom_inbound_event` 状态流转契约：

- 初始入站记录：`received`
- worker 开始处理：`processing`
- 成功完成：`done`
- 本次处理失败但仍可重试：`failed`
- 达到重试上限或判定不可恢复：`dead_letter`

死信转移基线：

- 处理失败后最多重试 2 次
- 第 2 次重试后仍失败，转入 `queue:dead_letter`
- 明确不可恢复的错误也可直接进入 `dead_letter`

### 5.6 测试要求

本阶段至少要覆盖：

- LLM provider 单测：
  - provider 工厂切换
  - 未知 provider 抛错
  - 请求构造正确
  - 正常响应能转成 `IntentResult` / `RerankResult`
  - 非 JSON 响应兜底
  - 超时和重试逻辑
  - fallback 没有硬编码最终用户文案
- Prompt 单测：
  - prompt 常量存在
  - 包含版本标记
  - 包含 token 预算注释
  - 包含严格 JSON 输出约束
  - 字段契约与 schema 对齐
- 存储测试：
  - 保存成功
  - URL 返回正确
  - 删除成功
  - 存在性检查正确
  - key 路径规则正确
- 企微测试：
  - 验签
  - 解密
  - XML 解析
  - 统一消息对象字段完整
  - `send_text` / `download_media` / `get_external_contact` 对响应的封装正确
  - `get_external_contact()` 的 `None` / 异常语义正确
- 契约测试：
  - 队列 key 命名和调用顺序已文档化
  - `wecom_inbound_event` 状态流转规则已文档化
  - 死信转移条件已文档化

## 6. 交付物

Phase 2 完成后，至少应交付：

- `backend/app/llm/prompts.py`
- `backend/app/llm/providers/qwen.py`
- `backend/app/llm/providers/doubao.py`
- `backend/app/llm/__init__.py`
- `backend/app/storage/local.py`
- `backend/app/storage/__init__.py`
- `backend/app/wecom/crypto.py`
- `backend/app/wecom/callback.py`
- `backend/app/wecom/client.py`
- 对应 `backend/tests/` 下的单元/集成测试
- 一份可供开发和测试复现的执行说明或 README 更新
- 一份面向 Phase 4 的消息契约说明，至少覆盖队列 key、限流/幂等调用约定和 `wecom_inbound_event` 状态流转

## 7. 前端需要做什么

本阶段前端无正式开发任务。

说明：

- Phase 2 的输出主要是后端基础设施契约
- 前端此时不做页面、不做联调、不做 mock 数据对接
- 如果后续 Phase 5/6 需要复用字段命名，以 `schemas/` 和本阶段接口契约文档为准

## 8. 验收标准

- 可通过统一工厂切换 LLM provider
- `llm/prompts.py` 已形成集中管理的 prompt 骨架
- prompt 包含版本标记、token 预算、严格 JSON 契约和兜底约束
- 本地存储可正常保存和删除附件
- 企微回调消息可被验签、解密并转换为统一消息对象
- 企微基础客户端方法可被 mock 并通过自动化测试
- Phase 4 依赖的消息基础契约已文档化并与现有 Redis / model 能力对齐
- 本阶段新增模块均具备自动化测试
- 代码没有越界实现到 Phase 3/4 的业务编排

## 9. 风险与备注

### 9.1 Phase 2 与 Phase 3 的边界

Phase 2 可以为 Phase 3 提供 prompt 骨架和 provider 能力，但不能提前把业务规则固化进 provider 或 wecom 层，例如：

- 不能把岗位/简历字段追问逻辑写进 provider
- 不能把 `criteria_patch` merge 逻辑写进 provider
- 不能把权限过滤逻辑写进 client 或 callback
- 不能把最终用户回复文案写死在 provider fallback 中

### 9.2 外部依赖风险

Phase 2 单测和集成测试应以 mock 为主，避免被真实 API key、企微认证级别、外网抖动阻塞开发。

其中：

- `doubao.py` 允许在没有真实 API Key 的情况下以“结构骨架 + mock 测试通过”交付
- 真实豆包 API 联调可在获取 Key 后补做，不阻塞 Phase 2 验收

### 9.3 完成后需要做的事

- ~~将本文档状态更新为 `done`~~ ✅
- ~~记录实际完成日期~~ ✅ 2026-04-12
- 输出对 Phase 3 的 handoff — 见下方 §10

## 10. Phase 3 Handoff

### 10.1 Prompt 骨架当前状态

文件：`backend/app/llm/prompts.py` (v1.0, 2026-04-12)

- `INTENT_SYSTEM_PROMPT` / `INTENT_USER_TEMPLATE`：结构完整，包含 9 种 intent 枚举、JSON 契约、fallback 规则、输出示例
- `RERANK_SYSTEM_PROMPT` / `RERANK_USER_TEMPLATE`：结构完整，包含 ranked_items/reply_text 字段说明
- **占位内容**：
  - few-shot 仅有 1 条最小示例，业务话术和多轮追问策略留给 Phase 3 细化
  - 字段抽取的详细映射规则（§7 字段清单 → structured_data）尚未嵌入 prompt
  - Reranker 的角色差异化表述（worker vs factory vs broker）仅在 prompt 中留了占位

### 10.2 错误兜底已定 vs 留给 Phase 3

**已定（Phase 2 锁定）：**
- JSON 解码失败 → `intent="chitchat"`, `confidence=0.0`
- JSON 合法但字段类型不匹配（如 `structured_data: "oops"`）→ 防御性类型校正后 fallback
- 未知 intent 值 → 按 `chitchat` 处理
- confidence 超出 [0,1] → clamp
- LLM 超时（含 1 次重试）→ 抛 `LLMTimeout`
- LLM HTTP 错误 (4xx/5xx) → 抛 `LLMError`
- Provider 层不硬编码最终用户回复文案

**留给 Phase 3：**
- "没太理解您的意思" 等面向用户的回复文案 → service 层决定
- "系统繁忙" 等降级回复 → service 层 catch `LLMTimeout`/`LLMError` 后生成
- 追问策略（missing_fields 触发逻辑）→ service 层实现
- 真实 LLM 效果指标和 prompt 调优

### 10.3 企微统一消息对象最终版

类：`backend/app/wecom/callback.WeComMessage` (dataclass)

| 字段 | 类型 | 说明 |
|---|---|---|
| `msg_id` | `str` | 企微消息 ID |
| `from_user` | `str` | 发送者 external_userid |
| `to_user` | `str` | 接收者（企业应用） |
| `msg_type` | `str` | text / image / voice / event |
| `content` | `str` | 文本内容（text）/ PicUrl（image）/ Event 名（event） |
| `media_id` | `str` | 图片/语音 media_id |
| `create_time` | `int` | Unix 时间戳 |

### 10.4 Phase 4 可直接复用的消息契约

文件：`docs/message-contract.md` (locked, 2026-04-12)

覆盖内容：
- Redis 队列 key 约定（`queue:incoming` / `queue:dead_letter`）
- Webhook 入口调用顺序（限流 → 幂等 → 入队）
- Worker 消费顺序（出队 → processing → done/failed/dead_letter）
- `wecom_inbound_event` 5 状态流转图
- 死信转移规则（最多重试 2 次）
- Redis 全 key 命名汇总表

### 10.5 已知限制

- `doubao.py` 仅验证了契约结构，未做真实 API 联调（等获取 Key 后补做）
- `LocalStorage` 仅实现本地文件系统，MinIO/OSS/COS 留给二期
- 企微封装未接真实环境，所有测试基于 mock
