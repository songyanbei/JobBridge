# Phase 3 测试用例集

> 基于：`collaboration/features/phase3-main.md`
> 配套：`collaboration/features/phase3-test-implementation.md`
> 状态：`draft`
> 创建日期：2026-04-13

## 1. 目标

本用例集面向 Phase 3 业务 service 层，覆盖：

- 需求完成性
- 功能符合性
- 权限与合规
- 异常与降级
- 会话状态一致性
- 回归与可复现性

## 2. 执行说明

- 优先在 Docker 容器或项目虚拟环境中执行
- 单元测试优先覆盖纯业务逻辑
- 集成测试覆盖数据库、Redis、跨 service 编排
- 若环境缺少 MySQL / Redis，应先标记“环境阻塞”，不得误判为功能通过

## 3. 用例矩阵

### 3.1 Prompt / Intent

| ID | 场景 | 前置条件 | 操作 | 预期 |
|---|---|---|---|---|
| P3-INT-001 | 显式命令优先于 LLM | 无 | 输入 `/帮助` | 返回 `intent=command`，`structured_data.command=help` |
| P3-INT-002 | `/重新找` 命令识别 | 无 | 输入 `/重新找` | 返回 `command=reset_search` |
| P3-INT-003 | `/删除我的信息` 命令识别 | 无 | 输入 `/删除我的信息` | 返回 `command=delete_my_data` |
| P3-INT-004 | `show_more` 同义语识别 | 无 | 输入“更多/换一批/下一页” | 返回 `intent=show_more`，不调用 LLM |
| P3-INT-005 | 普通搜索走 LLM | mock extractor | 输入“苏州找电子厂” | 返回 LLM 结果 |
| P3-INT-006 | structured_data 非法 key 清洗 | mock extractor 返回未知 key | 调用 `classify_intent()` | 未知 key 被丢弃并记录 warning |
| P3-INT-007 | criteria_patch 非法 op 清洗 | mock extractor 返回 `op=set` | 调用 `classify_intent()` | 非法 patch 被丢弃并记录 warning |
| P3-INT-008 | criteria_patch 非法 field 清洗 | mock extractor 返回未知 field | 调用 `classify_intent()` | 非法 patch 被丢弃并记录 warning |
| P3-INT-009 | missing_fields 敏感字段过滤 | mock extractor 返回 `ethnicity/has_tattoo` | 调用 `classify_intent()` | 敏感软字段不进入 `missing_fields` |
| P3-INT-010 | canonical key 约束 | mock extractor 返回中文字段名 | 调用 `classify_intent()` | 中文字段名不会进入业务层结果 |

### 3.2 Conversation

| ID | 场景 | 前置条件 | 操作 | 预期 |
|---|---|---|---|---|
| P3-CONV-001 | 新 session 初始化 | 无 | `create_session()` | role 正确，history 为空 |
| P3-CONV-002 | add patch 去重合并 | `city=["苏州"]` | add `["昆山","苏州"]` | 结果为 `["苏州","昆山"]` |
| P3-CONV-003 | update patch 替换 | 已有 salary 条件 | update 新值 | 原值被替换 |
| P3-CONV-004 | remove patch 删除单值 | `city=["苏州","昆山"]` | remove `昆山` | 仅剩 `["苏州"]` |
| P3-CONV-005 | remove null 删除整字段 | criteria 含 city | remove `value=null` | city 字段消失 |
| P3-CONV-006 | criteria 变化清空 snapshot | 已有 snapshot/shown_items | merge 后 digest 变化 | snapshot 清空，shown_items 清空 |
| P3-CONV-007 | criteria 未变化不清空 snapshot | add 重复值 | merge patch | snapshot 保留 |
| P3-CONV-008 | history 截断 | 连续追加 > 12 条 | `record_history()` | 仅保留最近 12 条 |
| P3-CONV-009 | broker_direction 设置成功 | broker session | `set_broker_direction(search_job)` | session 字段正确 |
| P3-CONV-010 | broker_direction 非 broker 拒绝 | worker/factory session | `set_broker_direction()` | 返回固定错误 |
| P3-CONV-011 | `/重新找` 不清 broker_direction | broker session 已有方向 | `reset_search()` | criteria/snapshot/history 清空，但 `broker_direction` 保留 |
| P3-CONV-012 | follow_up_rounds 计数 | round=0 | `increment_follow_up()` 两次 | 计数为 2 |

### 3.3 User

| ID | 场景 | 前置条件 | 操作 | 预期 |
|---|---|---|---|---|
| P3-USER-001 | 未注册用户自动注册 worker | DB 无该用户 | `identify_or_register()` | 新建 worker，`should_welcome=True` |
| P3-USER-002 | 未注册用户说“我要招人”也不升级角色 | DB 无该用户 | 调用识别 | 仍注册为 worker |
| P3-USER-003 | factory 首轮欢迎 | `last_active_at=NULL` | `identify_or_register()` | `should_welcome=True` |
| P3-USER-004 | broker 首轮欢迎 | `last_active_at=NULL` | `identify_or_register()` | `should_welcome=True` |
| P3-USER-005 | worker session 过期不重复欢迎 | 已存在 worker | 再次识别 | `should_welcome=False` |
| P3-USER-006 | blocked 拦截 | user.status=blocked | `check_user_status()` | 返回封禁提示 |
| P3-USER-007 | deleted 拦截 | user.status=deleted | `check_user_status()` | 返回受控提示，不自动恢复 |
| P3-USER-008 | 正常链路刷新 last_active_at | active user | `update_last_active()` | 时间更新 |
| P3-USER-009 | `/我的状态` 聚合查询 | 存在 job/resume | `get_user_status()` | 返回角色、状态、最近提交状态 |

### 3.4 Upload / Audit

| ID | 场景 | 前置条件 | 操作 | 预期 |
|---|---|---|---|---|
| P3-UP-001 | 岗位上传成功 | 必填字段齐全 | `process_upload(upload_job)` | 入库成功，写 `audit_status` 等字段 |
| P3-UP-002 | 简历上传成功 | 必填字段齐全 | `process_upload(upload_resume)` | 入库成功 |
| P3-UP-003 | 缺 1-2 个字段单句追问 | 缺少 1-2 项 | `process_upload()` | 返回单句追问 |
| P3-UP-004 | 缺 >=3 个字段列表追问 | 缺少 3 项以上 | `process_upload()` | 返回列表式追问 |
| P3-UP-005 | 连续两轮追问计数累加 | round=0 | 连续提交两次不完整 | `follow_up_rounds=2` |
| P3-UP-006 | 第三次不再追问 | round=2 且仍缺字段 | 再次提交 | 返回明确降级提示，不入库，并重置计数 |
| P3-UP-007 | 图片 key 留存 | 有 image_keys | 上传 | entity.images 正确保存 |
| P3-UP-008 | high 风险自动驳回 | 命中 high 词 | 上传 | `audit_status=rejected`，写 audit_log |
| P3-UP-009 | mid 风险进入待审 | 命中 mid 词 | 上传 | `audit_status=pending`，不进召回池 |
| P3-UP-010 | low 风险自动通过 | 命中 low 词 | 上传 | `audit_status=passed`，保留 reason/tag |
| P3-UP-011 | TTL 读取正确 | system_config 已配置 | 上传 | `expires_at` 按对应 TTL 计算 |

### 3.5 Search / Permission

| ID | 场景 | 前置条件 | 操作 | 预期 |
|---|---|---|---|---|
| P3-SEA-001 | worker 找岗位首批返回 <=3 条 | 至少 3 条 passed job | `search_jobs()` | 返回 1-3 条 |
| P3-SEA-002 | factory 找工人 | 至少 3 条 passed resume | `search_workers()` | 返回候选人列表 |
| P3-SEA-003 | broker 找岗位 | broker_direction=search_job | 搜索 | 走岗位方向 |
| P3-SEA-004 | broker 找工人 | broker_direction=search_worker | 搜索 | 走简历方向 |
| P3-SEA-005 | 0 召回不调 reranker | 查询无结果 | 搜索 | 直接返回空结果提示 |
| P3-SEA-006 | 候选不足触发一次宽松匹配 | 仅 1-2 条命中 | 搜索 | 只做一次薪资放宽 10% |
| P3-SEA-007 | 宽松匹配不跨轮记忆 | 连续两次独立搜索 | 两次搜索 | 每次仅在本次请求内决定是否放宽 |
| P3-SEA-008 | show_more 复用快照 | 已有 snapshot | `show_more()` | 不重跑全量检索 / rerank |
| P3-SEA-009 | show_more 跳过失效条目 | snapshot 中部分过期/下架 | `show_more()` | 跳过失效项继续往后取 |
| P3-SEA-010 | worker 视角不泄漏电话 | job owner 有电话 | `search_jobs()` | reply_text 不含电话 |
| P3-SEA-011 | worker 视角不泄漏歧视字段 | job 含 gender/age/民族字段 | `search_jobs()` | reply_text 不含相关字段 |
| P3-SEA-012 | factory/broker 看简历含电话 | owner 有电话 | `search_workers()` | reply_text 可含电话 |
| P3-SEA-013 | 简历缺电话给固定占位文案 | owner 无电话 | `search_workers()` | 返回“联系方式待补充” |
| P3-SEA-014 | 待审/驳回/过期/删除/下架项不进召回池 | 混合数据 | 搜索 | 仅 active + passed + 未过期有效项返回 |
| P3-SEA-015 | show_more 剩余数量文案准确 | snapshot 剩余 >1 | `show_more()` | “还有 N 个/位”中的 N 为真实剩余量 |

### 3.6 Delete / Compliance

| ID | 场景 | 前置条件 | 操作 | 预期 |
|---|---|---|---|---|
| P3-DEL-001 | 删除命令可独立执行 | user 存在 | `delete_user_data()` | 返回成功提示 |
| P3-DEL-002 | 删除时清 Redis session | session 已存在 | 删除 | session 被清空 |
| P3-DEL-003 | 删除时软删简历 | 用户有 resume | 删除 | `deleted_at` 被写入 |
| P3-DEL-004 | 删除时软删对话日志 | 用户有 conversation_log | 删除 | `expires_at=now` |
| P3-DEL-005 | 删除时标记用户状态 | active user | 删除 | `user.status=deleted` |
| P3-DEL-006 | 删除时写 conversation_log | 删除 | 查询日志 | 有系统日志记录 |
| P3-DEL-007 | 删除时写 audit_log | 删除 | 查询 audit_log | 有操作记录 |
| P3-DEL-008 | deleted 用户再次发消息受控提示 | 用户已 deleted | 再次进入 user 流程 | 返回提示，不静默忽略 |

### 3.7 服务级串联 Smoke

| ID | 场景 | 前置条件 | 操作 | 预期 |
|---|---|---|---|---|
| P3-SMOKE-001 | factory 上传岗位后 worker 可搜到 | MySQL + Redis 可用 | upload -> search_jobs | 链路贯通 |
| P3-SMOKE-002 | worker 上传简历后 factory 可搜到 | MySQL + Redis 可用 | upload -> search_workers | 链路贯通 |
| P3-SMOKE-003 | broker 切换方向再搜索 | MySQL + Redis 可用 | switch -> search | 方向 sticky 生效 |
| P3-SMOKE-004 | 搜索后 show_more 再翻页 | snapshot 已建立 | search -> show_more | 结果连续、无重复 |
| P3-SMOKE-005 | 删除后再次进入流程 | 删除已执行 | delete -> identify/check | 被拦截 |

## 4. 优先级

- P0：P3-INT-001~004，P3-USER-001/006/007，P3-UP-001/006/008/009，P3-SEA-001/005/008/010/014/015，P3-DEL-001~008
- P1：其余业务正确性与状态一致性用例
- P2：文案细节、展示细节、日志细节

## 5. 结论使用方式

测试执行后，建议按以下结构回填：

- 用例通过数 / 失败数 / 阻塞数
- 阻塞项是否为环境问题
- 真实功能缺陷清单
- 是否允许进入 Phase 4
