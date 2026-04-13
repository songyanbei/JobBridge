# Phase 3 开发 Checklist

> 基于：`collaboration/features/phase3-main.md`
> 配套实施文档：`collaboration/features/phase3-dev-implementation.md`
> 面向角色：后端开发
> 状态：`draft`
> 创建日期：2026-04-13

## A. 基线确认

- [ ] 已阅读 `collaboration/features/phase3-main.md`
- [ ] 已阅读 `collaboration/features/phase3-dev-implementation.md`
- [ ] 已确认本阶段只做业务 service 与 prompt 定稿，不做 webhook / worker / admin API
- [ ] 已确认 Phase 2 基础设施能力应直接复用，不重复造轮子
- [ ] 已确认 `search_criteria`、`criteria_patch.field`、`structured_data` 在代码内统一使用英文 canonical key

## B. Prompt 与 Intent

涉及文件：

- `backend/app/services/intent_service.py`
- `backend/app/llm/prompts.py`

- [ ] `intent_service.py` 已创建
- [ ] 显式命令识别已实现
- [ ] `show_more` 同义语识别已实现
- [ ] LLM 返回结果的 canonical key 校验已实现
- [ ] `criteria_patch.op` 非 `add/update/remove` 时会被拒绝或丢弃
- [ ] 未知字段 patch 会被丢弃并记录
- [ ] `llm/prompts.py` 已从骨架版升级为业务版
- [ ] prompt 版本号已升级（建议 `v2.0`）
- [ ] prompt 中已写明严格 JSON 输出
- [ ] prompt 中已写明禁止 markdown code block
- [ ] prompt 中已写明 canonical key 规则
- [ ] prompt 中已写明 `criteria_patch` 语义
- [ ] prompt 中已写明必填字段范围
- [ ] prompt 中未主动追问民族、纹身、健康证、禁忌
- [ ] prompt 至少覆盖工人找岗位 few-shot
- [ ] prompt 至少覆盖厂家上传 / 上传并检索 few-shot
- [ ] prompt 至少覆盖 follow_up / patch few-shot
- [ ] prompt 至少覆盖边界输入 few-shot
- [ ] `conversation_log.criteria_snapshot` 可记录 prompt 版本

## C. Conversation Service

涉及文件：

- `backend/app/services/conversation_service.py`

- [ ] `conversation_service.py` 已创建
- [ ] 已实现 session 读取
- [ ] 已实现 session 保存
- [ ] 已实现 `criteria_patch` merge
- [ ] `add` 仅用于列表型字段并做去重合并
- [ ] `update` 会整体替换标量或整列表
- [ ] `remove` 支持删除列表项或整字段
- [ ] criteria 有效变更后会清空快照
- [ ] criteria 有效变更后会清空 `shown_items`
- [ ] `query_digest` 计算稳定
- [ ] `history` 截断策略已固定（最多 12 条 message）
- [ ] 已实现 `record_shown()`
- [ ] 已实现 `build_snapshot()`
- [ ] 已实现 `get_next_candidate_ids()`
- [ ] 中介方向 sticky 状态已在 session 中实现（`broker_direction`）
- [ ] 上传追问轮数状态已在 session 中实现（`follow_up_rounds`）
- [ ] `/重新找` 会清空当前检索状态
- [ ] show_more 不会重新触发全量检索

## D. User Service

涉及文件：

- `backend/app/services/user_service.py`

- [ ] `user_service.py` 已创建
- [ ] 未预注册用户默认自动注册为 `worker`
- [ ] 未预注册但表达“我要招人”的用户仍按工人注册处理
- [ ] 厂家首次欢迎判定已实现
- [ ] 中介首次欢迎判定已实现
- [ ] 工人首次欢迎判定已实现
- [ ] `status=blocked` 会被短路拦截
- [ ] `status=deleted` 不会静默恢复
- [ ] deleted 用户人工恢复路径已明确留给 Phase 5 admin API
- [ ] 进入正常处理链路时会更新 `last_active_at`
- [ ] `/我的状态` 查询能力已实现
- [ ] `/删除我的信息` 的跨 service 编排入口由 `user_service` 发起
- [ ] 返回的用户上下文字段已固定

## E. Audit Service

涉及文件：

- `backend/app/services/audit_service.py`

- [ ] `audit_service.py` 已创建
- [ ] 已读取启用中的敏感词字典
- [ ] `high -> rejected` 已实现
- [ ] `mid -> pending` 已实现
- [ ] `low -> passed with tag` 已实现
- [ ] LLM 安全检查接入点已预留
- [ ] 安全接口不可用时有受控退化
- [ ] `passed` 会写 `audit_log`
- [ ] `rejected` 会写 `audit_log`
- [ ] `pending` 会把机器审核理由写入实体字段
- [ ] 未通过审核的条目不会进入召回池

## F. Permission Service

涉及文件：

- `backend/app/services/permission_service.py`

- [ ] `permission_service.py` 已创建
- [ ] 工人看岗位时电话已过滤
- [ ] 工人看岗位时详细地址已过滤
- [ ] 工人看岗位时歧视性展示字段已过滤
- [ ] 厂家/中介看简历时可获得电话
- [ ] 电话缺失时有固定占位文案
- [ ] 过滤结果以结构化数据返回
- [ ] 未过滤实体对象不会被直接交给最终回复拼装

## G. Upload Service

涉及文件：

- `backend/app/services/upload_service.py`

- [ ] `upload_service.py` 已创建
- [ ] 岗位上传流程已实现
- [ ] 简历上传流程已实现
- [ ] `upload_service` 消费 `IntentResult`，不重复调 LLM
- [ ] 图片只保存 key，不参与抽取
- [ ] 缺 1-2 个必填字段时会合并追问
- [ ] 缺 3 个及以上时会走列表式引导
- [ ] 连续追问上限 2 轮
- [ ] 连续第 3 轮不会继续追问
- [ ] `expires_at` 读取 `system_config`
- [ ] `audit_status` / `audit_reason` / `audited_by` / `audited_at` 已写入
- [ ] `passed` / `pending` / `rejected` 三种上传结果文案已区分

## H. Search Service

涉及文件：

- `backend/app/services/search_service.py`

- [ ] `search_service.py` 已创建
- [ ] 工人找岗位已实现
- [ ] 厂家找工人已实现
- [ ] 中介双向检索已实现
- [ ] 基础查询已过滤 `audit_status`
- [ ] 基础查询已过滤 `deleted_at`
- [ ] 基础查询已过滤 `expires_at`
- [ ] 岗位查询已过滤 `delist_reason`
- [ ] 基础查询已过滤 `user.status`
- [ ] 查询结果已补充关联用户信息
- [ ] rerank 前候选集上限已固定（50 条）
- [ ] 0 召回不会调用 Reranker
- [ ] 候选不足时只做一次薪资放宽 10%
- [ ] “只做一次”定义为单次搜索请求内的重试逻辑
- [ ] 本阶段未实现自动“邻近城市”扩展
- [ ] `match.top_n` 从 `system_config` 读取
- [ ] show_more 会跳过失效条目
- [ ] 最终文本基于过滤后结构化字段拼装
- [ ] 工人侧最终文本不泄漏电话、详细地址、歧视性字段

## I. 合规与命令基础能力

- [ ] `/重新找` 对应的 session reset 能力已可独立调用
- [ ] `/删除我的信息` 对应的删除流程已可独立调用
- [ ] 删除流程会立即清空 Redis session
- [ ] 删除流程会软删除简历
- [ ] 删除流程会软删除对话日志
- [ ] 删除流程会把 `user.status` 标记为 `deleted`
- [ ] 删除流程会写 `conversation_log`
- [ ] 删除流程会写 `audit_log`
- [ ] `/找岗位`、`/找工人` 的中介方向切换基础能力已具备
- [ ] `/我的状态` 返回账号状态与最近一次提交状态

## J. 自动化测试

- [ ] 已新增 `intent_service` 单测
- [ ] 已新增 `conversation_service` 单测
- [ ] 已新增 `user_service` 单测
- [ ] 已新增 `audit_service` 单测
- [ ] 已新增 `permission_service` 单测
- [ ] 已新增 `upload_service` 单测
- [ ] 已新增 `search_service` 单测
- [ ] 已新增 Phase 3 集成测试
- [ ] 已新增工人找岗位集成测试
- [ ] 已新增厂家找工人集成测试
- [ ] 已新增中介双向集成测试
- [ ] 已新增上传流程集成测试
- [ ] 已新增删除流程集成测试
- [ ] 已新增 show_more 集成测试
- [ ] 已新增 upload + search 服务级串联 smoke
- [ ] 已补至少一条 smoke 流程
- [ ] `backend/tests/README.md` 已更新

## K. 自测收口

- [ ] 工人找岗位流程可独立跑通
- [ ] 厂家找工人流程可独立跑通
- [ ] 中介切换方向后检索可独立跑通
- [ ] show_more 基于快照工作正常
- [ ] 审核待审/驳回条目不会进入召回池
- [ ] 工人侧不泄漏受限字段
- [ ] `/删除我的信息` 流程可跑通
- [ ] 本阶段代码没有越界到 webhook / worker / admin API / 前端
