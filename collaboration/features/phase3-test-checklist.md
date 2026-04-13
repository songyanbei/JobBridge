# Phase 3 测试 Checklist

> 基于：`collaboration/features/phase3-main.md`
> 配套实施文档：`collaboration/features/phase3-test-implementation.md`
> 面向角色：测试
> 状态：`draft`
> 创建日期：2026-04-13

## A. 测试前确认

- [ ] 已阅读 `collaboration/features/phase3-main.md`
- [ ] 已阅读 `collaboration/features/phase3-test-implementation.md`
- [ ] 已确认本阶段只测业务 service 与 prompt 定稿
- [ ] 已确认本阶段不测 webhook / worker / admin API / 前端页面
- [ ] 已拿到开发交付的代码、测试和运行说明

## B. Prompt 与 Intent 验证

- [ ] prompt 已从骨架版升级为业务版
- [ ] prompt 版本号已升级
- [ ] prompt 含 strict JSON 约束
- [ ] prompt 含禁止 markdown code block 约束
- [ ] prompt 含 canonical key 规则
- [ ] prompt 含 few-shot
- [ ] prompt 含 worker / factory / broker 角色上下文
- [ ] 显式命令优先级验证通过
- [ ] `show_more` 同义语识别验证通过
- [ ] `missing_fields` 不包含民族、纹身、健康证、禁忌
- [ ] `structured_data` 使用英文 canonical key
- [ ] `criteria_patch.field` 使用英文 canonical key
- [ ] `criteria_patch.op` 为非法值时会被丢弃并记录

## C. Conversation 验证

- [ ] session 可正常读取
- [ ] session 可正常保存
- [ ] `add` patch 行为正确
- [ ] `update` patch 行为正确
- [ ] `remove` patch 行为正确
- [ ] criteria 变化后会清空快照
- [ ] criteria 变化后会清空 `shown_items`
- [ ] 中介切换方向后 `broker_direction` session 状态正确
- [ ] 追问过程中 `follow_up_rounds` 计数正确
- [ ] `query_digest` 稳定
- [ ] history 截断策略正确
- [ ] `record_shown()` 去重正确
- [ ] show_more 不会触发全量检索
- [ ] `/重新找` 后检索状态已清空

## D. User 验证

- [ ] 未注册用户首次消息可自动注册为工人
- [ ] 未注册但表达招人需求的用户不会自动成为厂家/中介
- [ ] 厂家首次欢迎判定正确
- [ ] 中介首次欢迎判定正确
- [ ] 工人首次注册时会发送欢迎语
- [ ] session 过期后再次发消息不会重复欢迎
- [ ] blocked 用户会被短路拦截
- [ ] deleted 用户不会被静默恢复
- [ ] deleted 用户再次发消息会收到受控提示
- [ ] 正常处理链路会刷新 `last_active_at`
- [ ] `/我的状态` 可返回账号状态

## E. Upload 与 Audit 验证

- [ ] 岗位上传缺字段时会追问
- [ ] 简历上传缺字段时会追问
- [ ] 缺 1-2 个字段时为单句追问
- [ ] 缺 3 个及以上时为列表式引导
- [ ] 连续追问不超过 2 轮
- [ ] 图片 key 能留存
- [ ] `high` 风险词会自动拒绝
- [ ] `mid` 风险词会进入待人工审核
- [ ] `low` 风险词不会阻塞通过
- [ ] `passed` 条目可进入召回池
- [ ] `pending` 条目不会进入召回池
- [ ] `rejected` 条目不会进入召回池
- [ ] 自动通过 / 自动拒绝会写 `audit_log`

## F. Search 与 Permission 验证

- [ ] 工人找岗位流程可跑通
- [ ] 厂家找工人流程可跑通
- [ ] 中介切换 `/找岗位` 后检索方向正确
- [ ] 中介切换 `/找工人` 后检索方向正确
- [ ] 基础查询已过滤待审 / 驳回 / 过期 / 已下架 / 已删除数据
- [ ] 查询结果会补充关联用户信息
- [ ] 0 召回时不会调用 Reranker
- [ ] 候选不足时只做一次薪资放宽 10%
- [ ] 宽松匹配只执行一次
- [ ] 本阶段未自动做邻近城市扩展
- [ ] show_more 基于快照取下一批
- [ ] 快照失效条目会被跳过
- [ ] 工人侧结果不包含电话
- [ ] 工人侧结果不包含详细地址
- [ ] 工人侧结果不包含歧视性展示字段
- [ ] 厂家/中介侧结果可包含电话
- [ ] 电话缺失时有固定占位文案

## G. 删除流程与合规验证

- [ ] `/删除我的信息` 可独立执行
- [ ] 删除后 Redis session 被清空
- [ ] 删除后简历被软删除
- [ ] 删除后对话日志被软删除
- [ ] 删除后 `user.status=deleted`
- [ ] 删除动作写入 `conversation_log`
- [ ] 删除动作写入 `audit_log`

## H. 自动化测试验证

- [ ] `intent_service` 单测已提供
- [ ] `conversation_service` 单测已提供
- [ ] `user_service` 单测已提供
- [ ] `audit_service` 单测已提供
- [ ] `permission_service` 单测已提供
- [ ] `upload_service` 单测已提供
- [ ] `search_service` 单测已提供
- [ ] Phase 3 集成测试已提供
- [ ] upload + search 服务级串联 smoke 已提供
- [ ] smoke 流程已提供
- [ ] 运行方式可复现

## I. 越界检查

- [ ] 未提前实现 webhook 路由
- [ ] 未提前实现 worker 消费
- [ ] 未提前实现 admin API
- [ ] 未把业务判断写回 provider / storage / wecom 层
- [ ] 未引入向量检索 / OCR / 语音解析

## J. 测试结论输出

- [ ] 已输出通过 / 不通过结论
- [ ] 已输出缺陷清单
- [ ] 已输出复现步骤
- [ ] 已输出测试环境信息
- [ ] 已输出自动化测试结果
- [ ] 已输出是否建议进入 Phase 4
