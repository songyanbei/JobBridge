# Phase 2 开发 Checklist

> 基于：`collaboration/features/phase2-main.md`
> 配套实施文档：`collaboration/features/phase2-dev-implementation.md`
> 面向角色：后端开发
> 状态：`done`
> 创建日期：2026-04-12
> 完成日期：2026-04-12

## A. 基线确认

- [x] 已阅读 `collaboration/features/phase2-main.md`
- [x] 已确认本阶段只做基础设施适配与契约骨架，不做业务 service / webhook / worker
- [x] 已确认当前 `llm/base.py`、`storage/base.py` 只能作为抽象层使用
- [x] 已确认 `config.py` 中已有 LLM / WeCom / Storage 基础配置
- [x] 已确认 `core/redis_client.py` 队列能力复用，不重复造轮子

## B. 消息基础契约确认

- [x] 已文档化 `check_rate_limit()` 的调用入口约定
- [x] 已文档化 `check_msg_duplicate()` 的调用入口约定
- [x] 已文档化 `enqueue_message()` / `dequeue_message()` 的调用约定
- [x] 已确认入站队列 key 为 `queue:incoming`
- [x] 已确认死信队列 key 为 `queue:dead_letter`
- [x] 已文档化 `wecom_inbound_event` 的 5 个状态值
- [x] 已文档化状态流转：`received -> processing -> done / failed / dead_letter`
- [x] 已文档化失败重试上限为 2 次
- [x] 已文档化死信转移条件

> 产出：`docs/message-contract.md`

## C. LLM 骨架

涉及文件：

- `backend/app/llm/prompts.py`
- `backend/app/llm/__init__.py`
- `backend/app/llm/providers/qwen.py`
- `backend/app/llm/providers/doubao.py`

### C1. Prompt 骨架

- [x] 新建 `backend/app/llm/prompts.py`
- [x] 定义 Intent prompt 常量
- [x] 定义 Rerank prompt 常量
- [x] 每个 prompt 都带版本标记
- [x] 每个 prompt 都包含 token 预算注释
- [x] 每个 prompt 都明确要求严格 JSON 输出
- [x] prompt 字段说明与 `IntentResult` / `RerankResult` 对齐
- [x] prompt 中包含基础 fallback 规则说明
- [x] prompt 未散落在 provider 文件中

### C2. Provider 实现

- [x] 新建 `backend/app/llm/providers/qwen.py`
- [x] 新建 `backend/app/llm/providers/doubao.py`
- [x] 每个 provider 都实现 IntentExtractor
- [x] 每个 provider 都实现 Reranker
- [x] 使用 `httpx` 同步调用
- [x] 读取 `settings.llm_*` 配置
- [x] 超时逻辑已实现
- [x] 最多一次重试已实现
- [x] 正常响应可转换为统一结果对象
- [x] 非 JSON 响应有 fallback
- [x] `raw_response` 被保留
- [x] JSON 解析失败时 fallback 为 `intent="chitchat"`、`confidence=0.0`
- [x] 未知 intent 值按 `chitchat` 处理
- [x] provider 未硬编码最终用户回复文案
- [x] `doubao.py` 在无真实 Key 时也可通过 mock 测试交付
- [x] JSON 合法但字段类型不匹配时走统一 fallback（Codex R2 修复）
- [x] `httpx.HTTPStatusError` 统一包装为 `LLMError`（自审修复）

### C3. 工厂逻辑

- [x] 完成 `backend/app/llm/__init__.py`
- [x] 提供 `get_intent_extractor()`
- [x] 提供 `get_reranker()`
- [x] 默认读取 `settings.llm_provider`
- [x] 未知 provider 抛出明确错误
- [x] service 层未来无需直接 import 具体 provider

## D. 对象存储

涉及文件：

- `backend/app/storage/local.py`
- `backend/app/storage/__init__.py`

### D1. LocalStorage

- [x] 新建 `backend/app/storage/local.py`
- [x] `LocalStorage` 继承 `StorageBackend`
- [x] 实现 `save()`
- [x] 实现 `get_url()`
- [x] 实现 `delete()`
- [x] 实现 `exists()`
- [x] 支持分层 key
- [x] key 模板固定为 `{entity_type}/{record_id}/{uuid}.{ext}`
- [x] 自动创建中间目录
- [x] 路径规则可测试
- [x] Windows 盘符绝对路径逃逸已防御（Codex R1 修复）

### D2. Storage 工厂

- [x] 完成 `backend/app/storage/__init__.py`
- [x] 提供 `get_storage()`
- [x] 默认 provider 为 `local`
- [x] 未知 provider 抛出明确错误

### D3. 配置同步

- [x] 新增本地上传目录配置 `oss_local_dir`，已同步 `.env.example`
- [x] 新增本地 URL 前缀配置 `oss_local_url_prefix`，已同步 `.env.example`
- [x] 没有提前实现 MinIO / OSS / COS 的二期能力

## E. 企业微信基础封装

涉及文件：

- `backend/app/wecom/crypto.py`
- `backend/app/wecom/callback.py`
- `backend/app/wecom/client.py`

### E1. Crypto

- [x] 新建 `backend/app/wecom/crypto.py`
- [x] 实现验签函数
- [x] 实现解密函数
- [x] 输入输出类型清晰
- [x] 不包含业务判断
- [x] PKCS#7 padding 校验完整（Codex R1 修复）
- [x] 畸形密文统一抛 ValueError 而非底层异常（Codex R1 修复）

### E2. Callback

- [x] 新建 `backend/app/wecom/callback.py`
- [x] 实现 XML 解析
- [x] 定义统一消息对象
- [x] 统一消息对象包含 `msg_id`
- [x] 统一消息对象包含 `from_user`
- [x] 统一消息对象包含 `to_user`
- [x] 统一消息对象包含 `msg_type`
- [x] 统一消息对象包含 `content`
- [x] 统一消息对象包含 `media_id`
- [x] 统一消息对象包含 `create_time`
- [x] 文本消息可解析
- [x] 图片消息可解析

### E3. Client

- [x] 新建 `backend/app/wecom/client.py`
- [x] 实现 access token 获取封装
- [x] 实现 `send_text()`
- [x] 实现 `download_media()`
- [x] 实现 `get_external_contact()`
- [x] HTTP 细节未泄漏到外部调用方
- [x] 错误处理风格统一
- [x] `get_external_contact()` 返回 `None` 仅表示用户不存在或已删除
- [x] API 调用失败时抛异常，不返回 `None`
- [x] 非法 `agent_id` 初始化时 fail fast（Codex R1 修复）

## F. 自动化测试

### F1. LLM

- [x] 新增 LLM provider 工厂测试
- [x] 新增未知 provider 报错测试
- [x] 新增正常响应解析测试
- [x] 新增非 JSON fallback 测试
- [x] 新增超时重试测试
- [x] 新增 prompt 常量/版本/token 预算/JSON 契约测试
- [x] 新增 HTTPStatusError 统一异常测试
- [x] 新增 schema-shape 错误 fallback 测试（structured_data/missing_fields/criteria_patch/ranked_items 类型不匹配）
- [x] 新增 confidence 边界值测试（clamp、non-numeric）

### F2. Storage

- [x] 新增本地保存测试
- [x] 新增 URL 生成测试
- [x] 新增删除测试
- [x] 新增存在性测试
- [x] 新增 key 路径规则测试
- [x] 新增 Windows 盘符路径逃逸测试

### F3. WeCom

- [x] 新增验签测试
- [x] 新增解密测试
- [x] 新增 XML 解析测试
- [x] 新增统一消息对象字段完整性测试
- [x] 新增 `send_text()` 测试
- [x] 新增 `download_media()` 测试
- [x] 新增 `get_external_contact()` 测试
- [x] 新增畸形密文统一异常测试
- [x] 新增 WeComClient 初始化校验测试

## G. 自测 Checklist

- [x] `from app.llm import ...` 可正常导入
- [x] `from app.storage import ...` 可正常导入
- [x] `from app.wecom.client import WeComClient` 可正常导入
- [x] provider 工厂可切换 qwen / doubao
- [x] provider 配置错误时有明确异常
- [x] LocalStorage 可保存并删除临时文件
- [x] 统一消息对象可从 XML 解析生成
- [x] 自动化测试全部通过（200 unit tests passed）
- [x] Phase 4 所需的消息契约已文档化
- [x] 没有越界实现到 Phase 3 / Phase 4

## H. 最终交付

- [x] 新增代码文件已齐全（10 个源码文件）
- [x] 新增测试文件已齐全（8 个测试文件，200 tests）
- [x] 新增配置项 `.env.example` 已同步（`OSS_LOCAL_DIR`, `OSS_LOCAL_URL_PREFIX`）
- [x] 新增依赖 `requirements.txt` 已同步（`pycryptodome`）
- [x] 已知限制已记录（见 `phase2-dev-implementation.md` §9.6）
- [x] 可交付测试直接复现
