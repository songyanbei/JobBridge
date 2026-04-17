# Phase 5 测试 Checklist

> 基于：`collaboration/features/phase5-main.md`
> 配套实施文档：`collaboration/features/phase5-test-implementation.md`
> 面向角色：测试
> 状态：`draft`
> 创建日期：2026-04-16

## A. 测试前确认

- [ ] 已阅读 `collaboration/features/phase5-main.md`
- [ ] 已阅读 `collaboration/features/phase5-test-implementation.md`
- [ ] 已确认本阶段测试范围：admin API 全量 + 事件回传 API + DDL 变更 + 与 Phase 4 限流联动
- [ ] 已确认本阶段不测前端 / 定时任务 / nginx
- [ ] 已拿到开发交付的代码、Swagger UI 可访问 `/docs`
- [ ] 测试环境 MySQL（含 schema 升级）/ Redis / 后端服务 / Phase 4 worker 均已启动
- [ ] 默认管理员 `admin/admin123` 可登录
- [ ] 已准备测试数据：预注册厂家/中介、待审岗位/简历、被封禁用户、conversation_log、event_log
- [ ] `.env` 中 `EVENT_API_KEY` 已设置
- [ ] Phase 1~4 自动化测试仍然全部通过

## B. 数据层与基础设施

- [ ] `event_log` 表已创建
- [ ] `audit_log.action` 枚举已扩展至 `manual_edit / undo`
- [ ] `system_config` 已新增 5 个 key（event/audit/report/account 命名空间）
- [ ] `requirements.txt` 已含 jose / passlib / openpyxl
- [ ] `main.py` 已注册 admin_router、events_router
- [ ] `/health` 端点仍正常
- [ ] Swagger UI `/docs` 可访问，所有 admin 路由可见

## C. 登录与鉴权

### C.1 登录

- [ ] 正确密码 → 200 + token + password_changed
- [ ] 错误密码 → 40001
- [ ] 连续 ≥3 次失败 → 服务端 sleep 1 秒
- [ ] enabled=0 → 40301
- [ ] 成功后 `last_login_at` 更新
- [ ] 成功后失败计数清零

### C.2 当前用户与改密

- [ ] GET `/admin/me` 无 token → 40003
- [ ] GET `/admin/me` 错误 token → 40003
- [ ] GET `/admin/me` 过期 token → 40003
- [ ] GET `/admin/me` 正常 token → 200，无 password_hash
- [ ] PUT 改密旧密码错误 → 40001
- [ ] PUT 改密新密码 < 8 位 → 40101
- [ ] PUT 改密新旧相同 → 40101
- [ ] PUT 改密成功 → password_changed=1
- [ ] 改密成功后旧 token 仍可访问

## D. 审核工作台

### D.1 队列与详情

- [ ] 队列分页正常（page/size/total/pages 字段齐全）
- [ ] 队列筛选 status / target_type 生效
- [ ] 队列项含 `locked_by`
- [ ] 待审计数 `{job, resume, total}`
- [ ] 详情含：version / locked_by / risk_level / submitter_history / extracted_fields / field_confidence / trigger_rules

### D.2 软锁

- [ ] A lock 成功，Redis `audit_lock:{type}:{id}` = A.username
- [ ] B lock 同一条 → 40901，data.locked_by=A
- [ ] A unlock → Redis key 消失
- [ ] B unlock A 的锁 → key 不动
- [ ] Lock TTL 300 秒（可手测过期后再 lock）

### D.3 通过 / 驳回 / 编辑

- [ ] 通过：版本一致 → 成功，version+1
- [ ] 通过：版本不一致 → 40902
- [ ] 通过后写 audit_log `manual_pass`
- [ ] 驳回不带 reason → 40101
- [ ] 驳回 + block_user → 用户封禁 + audit_log 记录两类动作
- [ ] 编辑：版本不一致 → 40902
- [ ] 编辑：仅允许字段白名单
- [ ] 三类动作均触发 Undo Redis 写入

### D.4 Undo

- [ ] 30 秒内 undo → 状态恢复
- [ ] Undo 后写 audit_log `undo`
- [ ] Undo 后 Redis key 删除
- [ ] 30 秒后 undo → 40903
- [ ] 已 undo 后再次 undo → 40903

## E. 账号管理

### E.1 列表与详情

- [ ] 厂家列表分页 / 筛选正常
- [ ] 中介列表多 `can_search_jobs / can_search_workers`
- [ ] 工人列表只读
- [ ] 黑名单列表正常

### E.2 预注册

- [ ] 厂家预注册成功 → user 表新增 + audit_log
- [ ] 中介预注册成功 + 双向标记
- [ ] 重复 external_userid → 40904
- [ ] 角色不在 (factory/broker) → 40101

### E.3 Excel 批量导入

- [ ] 5 行成功 → success_count=5
- [ ] 第 3 行失败 → 全部回滚 + failed=[{row:3,...}]
- [ ] 单批 > `account.import_max_rows` → 40101
- [ ] 文件 > 2MB → 40101
- [ ] 公式注入字段被过滤或转义
- [ ] 非 .xlsx 扩展名 → 40101

### E.4 黑名单

- [ ] 封禁不带 reason → 40101
- [ ] 重复封禁 → 40904
- [ ] 封禁成功 → user.status=blocked + audit_log
- [ ] 解封不带 reason → 40101
- [ ] 解封成功 → user.status=active + audit_log

## F. 岗位 / 简历管理

### F.1 列表

- [ ] 筛选白名单生效（city / job_category / audit_status / delist_reason / salary_min/max / created_from/to）
- [ ] 排序白名单生效（白名单外 → 40101）
- [ ] 分页 size 上限 100
- [ ] 简历筛选维度（gender / age_min/max / expected_cities / expected_job_categories）

### F.2 编辑

- [ ] 编辑乐观锁版本一致 → 成功
- [ ] 编辑版本不一致 → 40902
- [ ] 不可修改字段（id / owner_userid / created_at / version） → 40101
- [ ] 写 audit_log `manual_edit`

### F.3 下架 / 延期 / 取消下架

- [ ] 下架 manual_delist → delist_reason=manual_delist
- [ ] 下架 filled → delist_reason=filled
- [ ] reason 不在枚举 → 40101
- [ ] 延期 15/30 天 → expires_at 增加
- [ ] 延期超过 ttl.job.days*2 → 截断或 40101（按实现）
- [ ] 取消下架（未过期）→ delist_reason=NULL
- [ ] 取消下架（已过期）→ 40904
- [ ] 三类动作均写 audit_log

### F.4 导出

- [ ] CSV 文件名带时间戳
- [ ] UTF-8 BOM 头
- [ ] 数据行不超过 10000
- [ ] 超过 10000 → 40101

## G. 字典管理

### G.1 城市

- [ ] 列表按省份分组
- [ ] 编辑 alias → 成功
- [ ] 修改 name 等其它字段 → 被忽略或 40101
- [ ] 写 audit_log

### G.2 工种

- [ ] CRUD 全部正常
- [ ] code 重复 → 40904
- [ ] name 重复 → 40904
- [ ] 删除被引用 → 40904
- [ ] 排序通过 sort_order 修改

### G.3 敏感词

- [ ] 单个新增 → word 重复 40904
- [ ] 删除单个 → 成功
- [ ] 批量导入返回 `{added, duplicated}`
- [ ] 批量去重正常
- [ ] 字典变更后 audit_service 缓存被清

## H. 系统配置

- [ ] 全部配置按命名空间分组返回
- [ ] 不暴露 .env 中的密钥字段
- [ ] PUT 不存在 key → 40401
- [ ] PUT int 类型不匹配 → 40101
- [ ] PUT bool 类型不匹配 → 40101
- [ ] PUT json 不可解析 → 40101
- [ ] 危险项变更（filter.* / llm.provider）→ audit_log + 缓存清理
- [ ] 修改后 `config_cache:{key}` 与 `config_cache:all` 被删除
- [ ] **修改 rate_limit.max_count 后 Phase 4 webhook 行为变化**

## I. 数据看板

- [ ] Dashboard 含 today / yesterday / trend_7d
- [ ] 第二次调用响应时间 < 50ms（缓存命中）
- [ ] Redis `report_cache:dashboard` 存在且 TTL ≤ 60
- [ ] 60 秒后再次调用触发重算
- [ ] trends `?range=7d` 返回 7 个点
- [ ] trends `?range=30d` 返回 30 个点
- [ ] trends `?range=custom&from=&to=` 自定义范围正常
- [ ] top `?dim=city|job_category|role` 正常
- [ ] funnel 5 个阶段 + 数值递减
- [ ] export CSV 可下载

## J. 对话日志

- [ ] 不带 userid → 40101
- [ ] 范围 > 30 天 → 40101
- [ ] 正常返回按时间倒序
- [ ] 筛选 direction / intent 生效
- [ ] 分页 size ≤ 200
- [ ] export CSV 含 criteria_snapshot

## K. 事件回传

- [ ] 缺失 X-Event-Api-Key → 40001
- [ ] 错误 key → 40001
- [ ] 正确 key + 全新 target → 写入 event_log，data.deduped=false
- [ ] 10 分钟内重复 → data.deduped=true，event_log 不新增
- [ ] 不同 target 不去重
- [ ] 写库失败 → 不阻塞回包，audit_log 记录失败原因

## L. 错误响应与全局异常

- [ ] pydantic 校验错误 → 40101 + data.fields
- [ ] 未捕获异常 → 50001
- [ ] HTTPException → 标准化包装
- [ ] 所有错误响应 code 在 §6.4 范围内

## M. audit_log 完整性抽查

针对以下动作分别确认 audit_log 至少 1 条：

- [ ] 审核 manual_pass
- [ ] 审核 manual_reject
- [ ] 审核 manual_edit
- [ ] 审核 undo
- [ ] 审核 reject + block_user 同时记录两类
- [ ] 厂家预注册
- [ ] 中介预注册
- [ ] Excel 批量导入成功
- [ ] 用户封禁 / 解封
- [ ] 岗位下架 / 延期 / 取消下架 / 编辑
- [ ] 简历下架 / 延期 / 编辑
- [ ] 字典编辑（城市 / 工种 / 敏感词）
- [ ] 危险配置项变更
- [ ] 事件回传写入失败

## N. 回归确认

- [ ] Phase 1~4 自动化测试仍然全部通过
- [ ] webhook 仍可接收消息
- [ ] worker 仍可消费
- [ ] 旧 audit_log 记录仍可读
- [ ] /health 仍正常
- [ ] Phase 4 限流配置端到端验证：修改后第 2 条消息被限流
- [ ] 数据库表结构无破坏性变更（仅新增 event_log 表 + 扩展 audit_action 枚举）
- [ ] Redis 基础能力（session、锁、限流、幂等、队列）未被 Phase 5 修改影响

## O. Swagger UI 巡检

- [ ] 50 个 endpoints 全部可见
- [ ] 每个 endpoint 含 summary
- [ ] 每个 endpoint 的 request/response schema 字段含描述
- [ ] 鉴权 endpoint 显示 OAuth2/Bearer 标识
- [ ] 在 Swagger UI 中通过 Authorize 按钮可一次性带入 token
- [ ] 在 Swagger UI 中可以完成：登录 → 调审核 → 调字典 → 调配置 完整路径
