# Phase 2 开发实施文档

> 基于：`collaboration/features/phase2-main.md`
> 面向角色：后端开发
> 状态：`done`
> 创建日期：2026-04-12
> 完成日期：2026-04-12

## 1. 开发目标

本阶段开发目标是把“会被后续业务层依赖的基础设施能力”先做成稳定骨架，而不是追求业务效果。开发时请始终记住：

- 我们在做“接口和适配层”，不是在做“主链路”
- 业务层未来依赖抽象，不依赖具体 provider
- 任何厂商相关的 HTTP、鉴权、解析细节都应被封装在基础设施层内部
- 即使某些基础能力在 Phase 1 已存在，本阶段仍要把后续使用契约写清

## 2. 当前代码现状

当前仓库已具备：

- `app/config.py` 中已存在 `llm_provider`、`llm_api_key`、`llm_api_base`、`wecom_*`、`oss_*`
- `app/core/redis_client.py` 已具备队列、幂等、限流等基础方法
- `app/llm/base.py` 已有 `IntentExtractor`、`Reranker`
- `app/storage/base.py` 已有 `StorageBackend`

当前缺失：

- `llm/prompts.py`
- `llm/providers/qwen.py`
- `llm/providers/doubao.py`
- `llm/__init__.py` 可用工厂
- `storage/local.py`
- `storage/__init__.py` 可用工厂
- `wecom/crypto.py`
- `wecom/callback.py`
- `wecom/client.py`
- 对应自动化测试

另外，虽然 `core/redis_client.py` 与 `wecom_inbound_event` 已在 Phase 1 交付，但 Phase 2 仍需把它们在主链路中的调用契约文档化，不能默认后续阶段“自然知道怎么用”。

## 3. 开发原则

### 3.1 依赖方向

- service 层以后只能依赖 `llm/base.py`、`storage/base.py`、`wecom/client.py` 暴露的稳定接口
- 不允许在 service 层直接 import `llm/providers/qwen.py`、`storage/local.py`
- 不允许在 `wecom/` 中夹带业务判断

### 3.2 可测性优先

- 每个 provider / client 都必须可 mock
- 外部 HTTP 调用应集中在少量方法内部
- 解析、兜底、异常转换应能脱离真实外网独立测试

### 3.3 Phase 2 边界

本阶段只实现基础设施，不要顺手做：

- `message_router.py`
- `worker.py`
- webhook 路由
- 上传/检索/权限/审核业务 service
- 真正的 prompt 调优和效果优化

但要补一类“文档契约工作”：

- 锁定 Redis 限流、幂等、入队、死信调用顺序
- 锁定 `wecom_inbound_event` 状态流转和重试规则

## 4. 建议开发顺序

### 第 1 步：先补齐工厂和常量骨架

先完成下面几件事，给后续开发一个稳定形状：

- `llm/prompts.py`：先定义 prompt 常量、版本号、JSON 契约文本、token 预算注释
- `llm/__init__.py`：先把工厂函数签名定下来
- `storage/__init__.py`：先把 `get_storage()` 签名定下来

这一步的目标是先锁定接口，而不是先写具体实现。

### 第 2 步：实现 LLM provider

建议优先实现一个 provider 再复制模式到第二个 provider，避免两个文件同时发散。

实现时要统一：

- 构造请求 payload 的方式
- 读取模型名和超时配置的方式
- 错误处理方式
- JSON 解析和 fallback 方式
- `raw_response` 的保留方式

推荐做法：

- 提炼少量内部私有 helper，减少两个 provider 的重复代码
- 如果两家响应格式差异较大，允许各文件独立解析，但外部输出必须统一

本阶段必须明确 fallback 边界：

- provider 层负责把异常响应转换为统一结果或统一异常
- JSON 解析失败时，`IntentExtractor` fallback 返回 `IntentResult(intent="chitchat", confidence=0.0, raw_response=原始输出)`
- 未知 intent 值按 `chitchat` 处理
- timeout 最多重试 1 次，这个规则写死在 provider 层
- 最终回复给用户的中文文案不属于 provider 层职责，留给 Phase 3 service 层

### 第 3 步：实现本地存储

`LocalStorage` 尽量做得朴素稳定：

- 只关心 key 到本地路径的映射
- 自动创建目录
- 对非法 key 或路径穿越做最小保护
- URL 规则固定且可测试

如果当前配置不够，允许新增最小配置项，但不要一口气把 OSS/MinIO 全做出来。

### 第 4 步：实现企微基础封装

建议按顺序做：

1. `crypto.py`
2. `callback.py`
3. `client.py`

原因：

- `callback.py` 依赖解密结果
- `client.py` 独立于回调链路，可并行但最好最后统一风格

开发要求：

- `crypto.py` 只做加解密和签名校验，不做业务分发
- `callback.py` 只做 XML 到统一对象的转换
- `client.py` 只做 HTTP 调用封装

同时要补一份“消息基础契约说明”：

- webhook 先限流，再幂等，再入 `queue:incoming`
- worker 从 `queue:incoming` 消费，失败重试后转 `queue:dead_letter`
- `wecom_inbound_event` 从 `received` 进入 `processing`，最终落到 `done` / `failed` / `dead_letter`

### 第 5 步：补齐测试，再做自测

不要等全部写完再补测试。建议每完成一块就补对应单元测试，最后再补少量集成测试。

## 5. 文件级实施要求

### 5.1 `backend/app/llm/prompts.py`

必须包含：

- `INTENT_PROMPT` 或等价命名常量
- `RERANK_PROMPT` 或等价命名常量
- 明确版本标记
- 明确 token 预算注释
- 明确“只输出 JSON，不要 markdown code block”
- 与 `IntentResult` / `RerankResult` 对齐的字段说明
- 基础 fallback 规则说明

建议直接在注释里写清：

- `IntentExtractor`：input `< 2000 tokens`，output `< 500 tokens`
- `Reranker`：input `< 4000 tokens`，output `< 1000 tokens`

本阶段允许：

- 用占位 few-shot 或结构示例

本阶段不允许：

- 把业务字段追问逻辑写死到无法调整
- 在多个 provider 文件里复制 prompt 文本

### 5.2 `backend/app/llm/providers/qwen.py` 与 `doubao.py`

必须包含：

- `IntentExtractor` 实现类
- `Reranker` 实现类
- 使用 `httpx` 的统一调用入口
- 超时配置读取
- 最多一次重试
- 响应解析
- 非 JSON fallback

建议统一约定：

- 类命名风格一致
- 抛出的异常风格一致
- 记录 `raw_response` 的方式一致

降级要求：

- `doubao.py` 在没有真实 API Key 时，允许只以结构骨架 + mock 测试通过交付
- 不允许因为豆包外部依赖不可用而阻塞整个 Phase 2

### 5.3 `backend/app/llm/__init__.py`

必须包含：

- provider 注册表
- `get_intent_extractor()`
- `get_reranker()`

要求：

- 默认读取 `settings.llm_provider`
- 未知 provider 抛出清晰错误
- 不能在这里写 prompt 或 HTTP 逻辑

### 5.4 `backend/app/storage/local.py`

必须包含：

- `LocalStorage(StorageBackend)`
- 对 `save/get_url/delete/exists` 的实现

要求：

- 支持分层 key
- 遵循统一 key 模板：`{entity_type}/{record_id}/{uuid}.{ext}`
- 第一层固定为实体类型，例如 `jobs` / `resumes` / `avatars`
- 第二层固定为记录 ID
- 文件名统一使用 UUID 或等价唯一值，避免覆盖
- 自动创建目录
- 返回值稳定
- 删除不存在文件时行为明确

### 5.5 `backend/app/storage/__init__.py`

必须包含：

- provider 注册表
- `get_storage()`

要求：

- 一期默认 `local`
- 未知 provider 抛出明确错误

### 5.6 `backend/app/wecom/crypto.py`

必须包含：

- 签名校验函数
- 解密函数
- 必要的内部 helper

要求：

- 不把 HTTP 参数解析和业务分发写进来
- 输入输出类型明确

### 5.7 `backend/app/wecom/callback.py`

必须包含：

- XML 解析函数
- 统一消息对象定义或导出

统一消息对象最少字段：

- `msg_id`
- `from_user`
- `to_user`
- `msg_type`
- `content`
- `media_id`
- `create_time`

要求：

- 文本、图片消息都能被解析
- 缺失字段时行为可预测

### 5.8 `backend/app/wecom/client.py`

必须包含：

- `send_text()`
- `download_media()`
- `get_external_contact()`

要求：

- 封装 token 获取和接口 URL
- 对接口响应做统一错误处理
- 返回值契约稳定

`get_external_contact()` 的语义必须固定：

- 返回 `None` = 用户不存在或已被删除
- 网络错误、鉴权失败、接口异常 = 抛异常
- 不允许把“用户不存在”和“接口失败”混在同一个 `None`

### 5.9 消息基础契约说明

虽然 Phase 2 不实现 webhook / worker，但开发需要同步产出一份可供 Phase 4 直接复用的基础契约说明，至少包含：

- webhook 调用顺序：
  - `check_rate_limit()`
  - `check_msg_duplicate()`
  - `enqueue_message("queue:incoming", payload)`
- worker 消费顺序：
  - `dequeue_message("queue:incoming")`
  - 写入/更新 `wecom_inbound_event`
  - 失败重试
  - 超过上限转 `enqueue_message("queue:dead_letter", payload)`
- `wecom_inbound_event` 状态机：
  - `received -> processing -> done`
  - `received -> processing -> failed`
  - `received -> processing -> dead_letter`
- 死信规则：
  - 最多重试 2 次
  - 不可恢复错误可直接进入死信

## 6. 推荐测试策略

开发自测建议按以下层次推进：

1. 纯单元测试
- prompt 常量
- provider 工厂
- 消息契约常量或说明文档检查
- storage 路径映射
- callback XML 解析

2. mock HTTP 单测
- LLM provider 正常/异常/超时
- WeCom client 正常/异常

3. 轻量集成测试
- 本地文件保存删除
- 配置驱动 provider 切换

本阶段不要求真实打外网，也不建议把测试依赖在真实 API key 上。

## 7. 自测通过标准

开发提交前，至少确认：

- 新增文件都已创建并能导入
- 工厂可返回正确实例
- 未知 provider 会抛错
- prompt 常量存在且带版本
- prompt 常量包含 token 预算注释
- 本地存储保存、读取、删除跑通
- 企微 XML 能转成统一消息对象
- LLM provider 的正常响应、超时重试、非 JSON fallback 都可被自动化测试覆盖
- provider fallback 没有硬编码最终用户回复文案
- `get_external_contact()` 的 `None` / 异常语义已固定
- 消息基础契约已整理完成，可直接 handoff 给 Phase 4
- 本阶段代码没有越界到 Phase 3/4

## 8. 交付要求

开发提交时应一并提供：

- 代码
- 自动化测试
- 如新增配置项，同步 `.env.example`
- 如新增运行说明，同步 `backend/tests/README.md` 或相关 README
- 已知限制列表，例如：
  - 哪些 prompt 仍是骨架
  - 哪些 provider 目前只验证了契约，未做真实联调

## 9. 实际交付记录

> 完成日期：2026-04-12

### 9.1 新增代码文件

| 文件 | 职责 |
|---|---|
| `app/llm/prompts.py` | Prompt 模板集中管理 (v1.0)，版本标记、token 预算、JSON 契约 |
| `app/llm/__init__.py` | LLM 工厂：`get_intent_extractor()` / `get_reranker()` |
| `app/llm/providers/_base.py` | Provider 公用 helper：HTTP 调用（1 次重试）、JSON 解析、多层 fallback |
| `app/llm/providers/qwen.py` | 通义千问 IntentExtractor + Reranker |
| `app/llm/providers/doubao.py` | 豆包 IntentExtractor + Reranker（骨架，mock 测试通过） |
| `app/storage/local.py` | 本地文件存储，key 分层、路径穿越保护（含 Windows 盘符逃逸防御） |
| `app/storage/__init__.py` | 存储工厂：`get_storage()` |
| `app/wecom/crypto.py` | 企微签名校验 + AES-256-CBC 加解密（含 PKCS#7 校验和统一异常） |
| `app/wecom/callback.py` | XML 解析 + `WeComMessage` 统一消息对象 |
| `app/wecom/client.py` | `WeComClient`：token 管理、send_text、download_media、get_external_contact |

### 9.2 新增测试文件 (200 unit tests)

| 文件 | 测试数 | 覆盖 |
|---|---|---|
| `test_llm_prompts.py` | 19 | 常量、版本、token 预算、JSON 契约、字段对齐 |
| `test_llm_factory.py` | 10 | 工厂切换、未知 provider 报错 |
| `test_llm_providers.py` | 33 | 解析、fallback、schema-shape 兜底、超时重试、HTTPStatusError |
| `test_storage.py` | 16 | 保存、URL、删除、存在性、路径规则、Windows 路径逃逸 |
| `test_wecom_crypto.py` | 12 | 签名校验、加解密往返、篡改密文统一异常 |
| `test_wecom_callback.py` | 11 | XML 解析、统一消息对象、字段完整性 |
| `test_wecom_client.py` | 15 | 初始化校验、token、send_text、download_media、get_external_contact |
| `test_message_contract.py` | 16 | 队列 key、状态流转、调用顺序文档验证 |

### 9.3 新增/变更配置

- `config.py` 新增 `oss_local_dir`、`oss_local_url_prefix`
- `.env.example` 同步新增
- `requirements.txt` 新增 `pycryptodome>=3.20,<4.0`

### 9.4 新增文档

- `docs/message-contract.md`：Phase 4 消息基础契约
- `phase2-main.md` §10：Phase 3 Handoff（prompt 占位、兜底边界、消息对象、已知限制）

### 9.5 Code Review 修复记录

| 轮次 | 发现 | 修复 |
|---|---|---|
| 自审 | `crypto.py` 无用 import、PKCS#7 dead code | 删除 |
| 自审 | confidence 超 [0,1] 触发 ValidationError | clamp |
| 自审 | HTTPStatusError 泄漏为原始 httpx 异常 | catch → LLMError |
| 自审 | `enqueue_message` 不支持死信队列 | 加 queue 参数 |
| Codex R1 | Windows 盘符路径逃逸 LocalStorage base_dir | `os.path.abspath` + `startswith` 二次校验 |
| Codex R1 | 畸形密文抛 `struct.error` 而非 ValueError | PKCS#7 校验 + 长度校验 + 统一 except |
| Codex R1 | 非法 agent_id 静默降级为 0 | 初始化时 fail fast |
| Codex R2 | JSON 合法但字段类型错误时 ValidationError 逃逸 | 三层防御：类型校正 → Pydantic 构造 → except 兜底 |

### 9.6 已知限制

- Prompt 为骨架版本 (v1.0)：few-shot 仅 1 条最小示例，业务话术留 Phase 3
- `doubao.py` 仅验证契约结构，未做真实 API 联调（等获取 Key 后补做）
- `LocalStorage` 仅实现本地文件系统，MinIO/OSS/COS 留二期
- 企微封装未接真实环境，所有测试基于 mock
