# Phase 3 测试实施文档

> 基于：`collaboration/features/phase3-main.md`
> 面向角色：测试
> 状态：`draft`
> 创建日期：2026-04-13

## 1. 测试目标

Phase 3 测试的重点，不是企微链路，也不是前端交互，而是确认“业务 service 本身已经正确、稳定、可复用”。

测试应重点回答以下问题：

- prompt 是否已经从骨架版升级为业务可用版
- intent、追问、patch merge、show_more 是否已经锁定稳定口径
- 上传、审核、检索、权限过滤、删除流程是否能独立跑通
- 工人、厂家、中介三类典型角色是否都能通过 service 层完成闭环
- 本阶段是否仍存在会导致 Phase 4 message_router 接不上去的歧义

## 2. 当前现状与测试重点

当前基础现状决定了本阶段测试策略：

- provider、storage、wecom、Redis 基础能力已经在 Phase 2 测过
- Phase 3 新增的主要是业务编排和业务约束
- 真实企微、真实 LLM 效果指标不是本阶段测试通过前提

因此本阶段测试重点转为：

1. 业务逻辑正确
2. 会话状态正确
3. 权限与合规正确
4. prompt 业务契约正确
5. 典型流程可复现

## 3. 本阶段测试范围

### 3.1 必测范围

1. Prompt 与 Intent
- 业务版 prompt 是否成型
- 显式命令优先级
- show_more 同义语判定
- `missing_fields` 口径
- canonical key 命名

2. Conversation
- session 读写
- `criteria_patch` merge
- `query_digest`
- 快照生成与失效
- `shown_items` 去重
- `/重新找`

3. User
- 工人自动注册
- 厂家/中介首次欢迎判定
- blocked / deleted 状态处理
- `last_active_at` 更新
- `/我的状态`

4. Upload + Audit
- 岗位上传入库
- 简历上传入库
- 缺失字段追问
- 图片 key 留存
- 自动通过 / 自动拒绝 / 待人工审核

5. Search + Permission
- 工人找岗位
- 厂家找工人
- 中介双向检索
- 0 召回
- 候选不足薪资放宽
- show_more 快照翻页
- 工人侧字段脱敏

6. 删除流程
- `/删除我的信息`
- session 清空
- 简历软删除
- 对话日志软删除
- 用户状态变更

### 3.2 不测范围

- webhook 路由
- worker 队列消费
- 企微真实入站 / 出站联调
- admin API
- 前端页面
- 向量检索
- OCR / 语音 / 文件解析

## 4. 测试数据准备建议

建议测试夹具至少覆盖以下数据：

### 4.1 用户数据

- `worker` 活跃用户
- `factory` 活跃用户
- `broker` 活跃用户
- `blocked` 用户
- `deleted` 用户

### 4.2 字典与配置

- 启用中的城市字典
- 启用中的工种字典
- 敏感词字典（high / mid / low 至少各一条）
- `match.top_n`
- `ttl.job.days`
- `ttl.resume.days`
- `filter.enable_gender`
- `filter.enable_age`
- `filter.enable_ethnicity`

说明：

- 上述 key 均为项目当前 `backend/sql/seed.sql` 中已存在的配置项，不是测试侧新增约定

### 4.3 岗位 / 简历数据

- `passed` 岗位
- `pending` 岗位
- `rejected` 岗位
- 已下架岗位
- 已过期岗位
- `passed` 简历
- `pending` 简历
- `rejected` 简历
- 电话缺失的工人简历

### 4.4 会话数据

- 无 session 用户
- 有活跃 session 用户
- 有 candidate snapshot 用户
- 已过期 session 用户

## 5. 测试方法建议

### 5.1 Prompt 与 Intent

建议验证：

- prompt 版本号已升级
- prompt 中包含 worker / factory / broker 上下文
- prompt 中包含 canonical key 规则
- prompt 中包含 strict JSON 规则
- prompt 中包含 few-shot
- 显式命令优先于自然语言
- `show_more` 不依赖 LLM 也可被正确识别
- `missing_fields` 不包含民族、纹身、健康证、禁忌
- `structured_data` / `criteria_patch` 使用英文 key
- `criteria_patch.op` 若为非法值（如 `set`、`replace`），会被丢弃并记录

### 5.1.1 场景模板 A：工人首次求职

Given：

- 数据库中存在至少 3 条 `passed`、未过期、未下架、城市为苏州市、工种为电子厂的岗位
- 用户 `external_userid=u_worker_001` 不存在

When：

- 调用 `user_service` 识别用户
- 调用 `intent_service` 处理文本“苏州找电子厂，5000以上”
- 调用 `search_service.search_jobs()`

Then：

- 用户被自动注册为 `worker`
- 首次欢迎判定为真
- 返回首批不超过 3 条岗位
- 结果中不包含电话、详细地址、歧视性字段

### 5.1.2 场景模板 B：岗位上传后服务级串联检索

Given：

- 存在 `factory` 用户 `u_factory_001`
- 数据库中存在至少 2 条符合招聘条件的 `passed` 简历

When：

- 调用 `intent_service` 解析上传岗位文本
- 调用 `upload_service` 完成岗位入库
- 继续以同一份结构化条件调用 `search_service.search_workers()`

Then：

- 岗位成功写入库
- 若审核为 `passed`，可拿到工人检索结果
- 若审核为 `pending/rejected`，则岗位不进入召回池，但串联测试仍能验证上传结果分支

### 5.1.3 场景模板 C：追问不超过 2 轮

Given：

- 存在 `worker` 用户
- `follow_up_rounds=0`

When：

- 连续三次提交都无法补齐上传场景的必填字段

Then：

- 前两次返回追问
- 第三次不再继续追问，也不会按残缺条件入库，而是返回明确的降级提示

### 5.2 Conversation

建议验证：

- 初始 session 创建正常
- `add` / `update` / `remove` 三种 patch 行为正确
- criteria 改变后会清空快照和 `shown_items`
- `query_digest` 稳定且可复现
- `history` 截断正常
- `show_more` 只取快照剩余项，不触发全量检索
- `/重新找` 后检索状态已清空
- `broker_direction` 在中介切换方向后保持正确
- `follow_up_rounds` 会随追问次数正确累计与清零

### 5.3 User

建议验证：

- 未注册用户首次消息自动注册为工人
- 未注册用户说“我要招人”时不会被自动建成厂家/中介
- 厂家首次触达会命中欢迎判定
- 中介首次触达会命中欢迎判定
- `blocked` 用户被短路
- `deleted` 用户不会被自动恢复
- `deleted` 用户再次发消息会得到受控提示，不是静默忽略
- 正常处理链路会刷新 `last_active_at`

### 5.4 Upload 与 Audit

建议验证：

- 上传岗位时缺失必填字段会追问
- 上传简历时缺失必填字段会追问
- 缺 1-2 个字段时为单句追问
- 缺 3 个及以上时为列表式引导
- 连续追问不超过 2 轮
- 图片 key 能成功留存
- `high` 风险词会自动拒绝
- `mid` 风险词会进入待人工审核
- `low` 风险词不会阻塞入池，但会保留标记
- `pending` / `rejected` 条目不会进入召回池

### 5.5 Search 与 Permission

建议验证：

- 工人找岗位能返回 3 条以内首批结果
- 厂家找工人能返回结果并带电话
- 电话缺失的简历仍可返回，但有占位文案
- 中介切换 `/找岗位` 与 `/找工人` 后结果方向正确
- 0 召回时不调用 Reranker
- 候选不足时只做一次薪资放宽 10%
- 宽松匹配不会在同一次搜索里反复执行多次
- Phase 3 不会自动做邻近城市扩展
- show_more 只取快照余量
- 快照中条目失效时会跳过并继续向后取
- 工人侧结果不出现电话、详细地址、歧视性字段

### 5.5.1 upload + search 串联 smoke

建议补一条服务级 smoke：

- 先调 `upload_service`
- 再调 `search_service`
- 不要求包装成最终 `upload_and_search` 命令
- 目标是确认 Phase 4 router 接入前，两个 service 的串联能力已具备

### 5.6 删除流程

建议验证：

- `/删除我的信息` 可独立执行
- 执行后 Redis session 被清空
- 简历被软删除
- 对话日志被软删除
- `user.status=deleted`
- 删除动作写入 `conversation_log`
- 删除动作写入 `audit_log`

## 6. 缺陷判定口径

出现以下任一情况，应判定 Phase 3 不通过：

- `search_criteria` / `criteria_patch` 在代码内仍混用中英文字段名
- 工人侧结果泄漏电话、详细地址或歧视性字段
- `show_more` 重新触发全量检索或 rerank
- 待审 / 驳回条目进入召回池
- `/删除我的信息` 只改用户状态、不清 session / 简历 / 日志
- prompt 仍停留在骨架版，没有业务 few-shot 和 canonical key 约束
- 上传追问超过 2 轮仍继续追问
- 0 召回仍调用 Reranker
- 候选不足时反复多次放宽条件，导致结果不可预测
- 中介方向切换行为不稳定
- 业务逻辑明显越界到 webhook / worker / admin API

## 7. 推荐测试顺序

1. 先跑纯单元测试
- prompt / intent
- conversation
- user
- audit
- permission

2. 再跑 service 单元测试
- upload
- search
- delete flow

3. 再跑集成测试
- 工人找岗位
- 厂家找工人
- 中介双向
- 上传与检索串联

4. 最后跑 smoke 流程
- 至少验证 1 条完整工人流程
- 至少验证 1 条完整厂家流程

## 8. 运行方式建议

开发交付时，需同步把 Phase 3 的运行方式补入 `backend/tests/README.md`。

测试文档层面的最低要求：

- 优先在 Docker 容器内运行，项目本地虚拟环境裸跑作为可选补充方式
- 如提供本地裸跑命令，以 PowerShell 为主即可；不要求为了测试文档专门处理多套跨平台 shell 差异
- smoke 流程入口清晰可运行

## 9. 测试输出要求

测试结束后，请至少输出：

- 通过 / 不通过结论
- 缺陷清单
- 复现步骤
- 测试环境说明
- 自动化测试执行结果
- 是否建议进入 Phase 4

## 10. 对开发的交付要求

测试开始前，应确认开发至少已交付：

- `backend/app/services/` 下 Phase 3 对应源码
- 对应单元测试和集成测试
- README 或运行说明更新
- 如新增配置项，已同步 `.env.example` / `seed.sql`
- 已知限制说明
