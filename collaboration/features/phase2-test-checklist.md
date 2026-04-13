# Phase 2 测试 Checklist

> 基于：`collaboration/features/phase2-main.md`
> 配套实施文档：`collaboration/features/phase2-test-implementation.md`
> 面向角色：测试
> 状态：`passed`
> 创建日期：`2026-04-12`
> 更新日期：`2026-04-13`

## A. 测试前确认

- [x] 已阅读 `collaboration/features/phase2-main.md`
- [x] 已阅读 `collaboration/features/phase2-test-implementation.md`
- [x] 已确认本阶段只测基础设施适配与契约骨架
- [x] 已确认本阶段不测业务 service / webhook / worker / admin API
- [x] 已拿到开发交付的代码、测试和运行说明

## B. 消息基础契约核对

- [x] 已文档化 `check_rate_limit()` 的调用约定
- [x] 已文档化 `check_msg_duplicate()` 的调用约定
- [x] 已文档化 `enqueue_message()` / `dequeue_message()` 的调用约定
- [x] 已确认 `queue:incoming` 命名
- [x] 已确认 `queue:dead_letter` 命名
- [x] 已文档化 `wecom_inbound_event` 的 5 个状态值
- [x] 已文档化状态流转：`received -> processing -> done / failed / dead_letter`
- [x] 已文档化最大重试次数为 2
- [x] 已文档化死信转移条件

## C. 代码结构核对

- [x] `backend/app/llm/prompts.py` 已存在
- [x] `backend/app/llm/providers/qwen.py` 已存在
- [x] `backend/app/llm/providers/doubao.py` 已存在
- [x] `backend/app/llm/__init__.py` 已存在并暴露工厂
- [x] `backend/app/storage/local.py` 已存在
- [x] `backend/app/storage/__init__.py` 已存在并暴露工厂
- [x] `backend/app/wecom/crypto.py` 已存在
- [x] `backend/app/wecom/callback.py` 已存在
- [x] `backend/app/wecom/client.py` 已存在

## D. LLM 验证

### D1. 工厂与导入

- [x] `get_intent_extractor()` 可导入
- [x] `get_reranker()` 可导入
- [x] 默认 provider 可从配置读取
- [x] 显式指定 provider 时可切换实现
- [x] 未知 provider 会抛出明确错误

### D2. Provider 行为

- [x] provider 正常响应可转换为 `IntentResult`
- [x] provider 正常响应可转换为 `RerankResult`
- [x] 非 JSON 响应会走 fallback
- [x] fallback 结果可预测
- [x] `raw_response` 被保留
- [x] 超时后会触发一次重试
- [x] 重试后仍失败时行为符合实现约定
- [x] provider fallback 未硬编码最终用户回复文案
- [x] `doubao.py` 在无真实 Key 时仍可通过 mock 测试验收
- [x] JSON 合法但字段类型不合法时会走统一 fallback / defensive coercion，不泄漏底层校验异常

### D3. Prompt 骨架

- [x] Intent prompt 常量存在
- [x] Rerank prompt 常量存在
- [x] 含版本标记
- [x] 含 token 预算注释
- [x] 含严格 JSON 输出约束
- [x] 含禁止 markdown code block 约束
- [x] 字段要求与 DTO 契约一致

## E. Storage 验证

- [x] `get_storage()` 可导入
- [x] 默认 storage provider 为 `local`
- [x] 未知 storage provider 会抛出明确错误
- [x] `save()` 后文件实际存在
- [x] `get_url()` 返回值稳定
- [x] `exists()` 行为正确
- [x] `delete()` 后文件消失
- [x] 删除不存在文件时行为符合实现约定
- [x] 多级 key 路径可正常工作
- [x] key 命名符合 `{entity_type}/{record_id}/{uuid}.{ext}`
- [x] Windows 绝对路径与路径穿越场景已被拒绝，文件不会逃逸存储根目录

## F. WeCom 验证

### F1. Crypto

- [x] 验签函数可正常调用
- [x] 合法签名验证通过
- [x] 非法签名验证失败
- [x] 解密函数可正常调用
- [x] 畸形密文、错误 padding、截断密文会抛出受控 `ValueError`

### F2. Callback

- [x] XML 可解析为统一消息对象
- [x] 文本消息可解析
- [x] 图片消息可解析
- [x] 统一消息对象包含 `msg_id`
- [x] 统一消息对象包含 `from_user`
- [x] 统一消息对象包含 `to_user`
- [x] 统一消息对象包含 `msg_type`
- [x] 统一消息对象包含 `content`
- [x] 统一消息对象包含 `media_id`
- [x] 统一消息对象包含 `create_time`

### F3. Client

- [x] `send_text()` 可测试
- [x] `download_media()` 可测试
- [x] `get_external_contact()` 可测试
- [x] client 可通过 mock HTTP 方式验证
- [x] client 错误处理行为明确
- [x] `get_external_contact()` 返回 `None` 仅表示用户不存在或已删除
- [x] API 调用失败时抛异常，不返回 `None`
- [x] 非法 `wecom_agent_id` 会快速失败，不再静默降级为 `0`

## G. 自动化测试验证

- [x] LLM 相关单元测试全部通过
- [x] Storage 相关测试全部通过
- [x] WeCom 相关测试全部通过
- [x] 本阶段新增测试不依赖真实外网
- [x] 本阶段新增测试结果可复现
- [x] 运行说明已覆盖 Linux/macOS、PowerShell、CMD
- [x] 单元测试通过：`200 passed`
- [x] 集成测试通过：`17 passed`
- [x] Phase 2 复合压测通过，队列残留 / 死信 / 错误数均为 `0`

## H. 越界检查

- [x] 未提前实现业务 service
- [x] 未提前实现 webhook 路由
- [x] 未提前实现 worker 消费逻辑
- [x] 未把业务判断写进 provider / storage / wecom 层
- [x] 未把 prompt 效果调优当成 Phase 2 验收条件

## I. 缺陷判定

- [x] 若工厂不能切换 provider，则判定不通过
- [x] 若 prompt 缺少严格 JSON 契约，则判定不通过
- [x] 若 prompt 缺少 token 预算约束，则判定不通过
- [x] 若非 JSON 时无 fallback，则判定不通过
- [x] 若 LocalStorage 不稳定，则判定不通过
- [x] 若 WeCom 不能形成统一消息对象，则判定不通过
- [x] 若消息基础契约未文档化，则判定不通过
- [x] 若基础设施模块无法 mock，则判定不通过
- [x] 若出现明显的 Phase 3/4 越界实现，则判定不通过
- [x] 上述阻塞项本轮验收均未触发

## J. 测试执行记录

- [x] 复核日期：`2026-04-13`
- [x] 复核范围：LLM、Prompt、Storage、WeCom、Redis 契约、自动化测试、压测
- [x] 单元测试命令：`.\.venv\Scripts\pytest.exe tests\unit -q`
- [x] 集成测试命令：`$env:RUN_INTEGRATION='1'; .\.venv\Scripts\pytest.exe tests\integration -q`
- [x] 压测命令：`.\.venv\Scripts\python.exe tests\perf\phase2_pressure.py --messages 2400 --ingress-workers 32 --consumer-workers 16 --client-iterations 1800 --client-workers 48`
- [x] 单元测试结果：`200 passed`
- [x] 集成测试结果：`17 passed`
- [x] 压测结果：ingress 吞吐 `1215.82 msg/s`，consumer 吞吐 `1198.95 msg/s`，mock WeCom client 吞吐 `10260.32 req/s`

## K. 测试结论输出

- [x] 输出通过 / 不通过结论
- [x] 输出缺陷清单
- [x] 输出复现步骤
- [x] 输出测试环境信息
- [x] 输出自动化测试结果
- [x] 输出是否可进入 Phase 3
- [x] 当前结论：`通过`
- [x] 当前建议：`可进入 Phase 3`
