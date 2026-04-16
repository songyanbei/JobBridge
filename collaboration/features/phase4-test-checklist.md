# Phase 4 测试 Checklist

> 基于：`collaboration/features/phase4-main.md`
> 配套实施文档：`collaboration/features/phase4-test-implementation.md`
> 面向角色：测试
> 状态：`draft`
> 创建日期：2026-04-14

## A. 测试前确认

- [ ] 已阅读 `collaboration/features/phase4-main.md`
- [ ] 已阅读 `collaboration/features/phase4-test-implementation.md`
- [ ] 已确认本阶段测试范围：webhook / worker / message_router / 10 条命令 / E2E
- [ ] 已确认本阶段不测 admin API / 前端页面 / 外部定时任务
- [ ] 已拿到开发交付的代码和模拟企微回调脚本
- [ ] 测试环境 MySQL / Redis / 后端服务 / Worker 进程均已启动
- [ ] 种子数据已导入（管理员、城市字典、工种字典、敏感词）
- [ ] 测试数据已准备（预注册厂家、预注册中介、已发布岗位、已提交简历）
- [ ] Phase 1~3 自动化测试仍然通过

## B. Webhook 回调链路

### B.1 验签与解密

- [ ] 正确签名 → 返回 200
- [ ] 错误签名 → 返回 403
- [ ] 消息体损坏 → 返回 200（不触发企微重试），有 error log
- [ ] GET 验证端点返回解密后 echostr

### B.2 幂等检查

- [ ] 全新 MsgId → 正常入队 + 写 inbound_event
- [ ] 重复 MsgId → 直接返回 200，不入队，不新增 inbound_event
- [ ] 幂等窗口 10 分钟内有效
- [ ] Redis 不可用时降级为 MySQL UNIQUE 约束兜底

### B.3 用户级限流

- [ ] 同一用户 10s 内 ≤ 5 条 → 全部入队
- [ ] 同一用户 10s 内第 6 条 → 不入队，不写 inbound_event
- [ ] 被限流时返回 200
- [ ] 被限流时异步回复限流提示
- [ ] 不同用户互不影响
- [ ] 限流窗口过后恢复正常

### B.4 入站事件与入队

- [ ] 正常消息写入 `wecom_inbound_event` 表，status=received
- [ ] 入队消息格式正确（JSON 含 msg_id、from_userid、msg_type、content、inbound_event_id）
- [ ] 响应时间 < 100ms

## C. Worker 消费与处理

### C.1 正常消费

- [ ] Worker 启动后可消费 `queue:incoming`
- [ ] 消费后 inbound_event: received → processing → done
- [ ] `worker_started_at` 和 `worker_finished_at` 正确记录

### C.2 分布式锁

- [ ] 同一用户两条消息串行处理
- [ ] 不同用户消息可并行处理（如 Worker 有多实例）
- [ ] 锁超时释放正常（30s TTL）

### C.3 错误重试

- [ ] 处理异常 → retry_count +1 → 重入队列
- [ ] retry_count < 2 → 继续重试
- [ ] retry_count >= 2 → 进入 dead_letter
- [ ] 死信消息 inbound_event status=dead_letter
- [ ] 死信消息 error_message 已记录
- [ ] 死信消息用户收到"系统繁忙"回复

### C.4 心跳

- [ ] Worker 启动后 60s 内出现 `worker:heartbeat:{pid}` key
- [ ] key 的 TTL 为 120s
- [ ] Worker 停止后 120s 内 key 自动过期

### C.5 启动自检

- [ ] DB 中有 status=processing 的记录 → Worker 启动后自动恢复入队
- [ ] 恢复的消息能正常被重新处理

### C.6 优雅退出

- [ ] 发送 SIGTERM 后 Worker 不立即崩溃
- [ ] 当前消息处理完成后 Worker 退出
- [ ] 未处理的队列消息不丢失

## D. 消息路由

### D.1 消息类型分流

- [ ] 文本消息 → 进入文本主链路处理
- [ ] 图片消息 → 下载留存 + 回复确认
- [ ] 语音消息 → 回复"暂不支持语音，请发送文字"
- [ ] 文件消息 → 回复"暂不支持文件，请直接用文字描述"
- [ ] event 类型 → 记录日志，不回复

### D.2 用户状态拦截

- [ ] blocked 用户 → 回复封禁提示
- [ ] deleted 用户 → 回复删除状态提示
- [ ] 正常用户 → 继续处理
- [ ] 正常用户 `last_active_at` 被更新

### D.3 首次欢迎

- [ ] 新工人首次消息 → 自动注册 → 工人欢迎语
- [ ] 预注册厂家首次消息 → 厂家欢迎语
- [ ] 预注册中介首次消息 → 中介欢迎语
- [ ] session 过期后再次发消息不重复欢迎

### D.4 意图分发

- [ ] upload_job → 调用 upload_service 处理
- [ ] upload_resume → 调用 upload_service 处理
- [ ] search_job → criteria 更新 + 检索 + 权限过滤 + 格式化回复
- [ ] search_worker → 同上
- [ ] upload_and_search → 先上传后检索
- [ ] follow_up → merge_criteria_patch + 重新检索
- [ ] show_more → search_service.show_more() 从快照取下一批
- [ ] chitchat → 返回引导语
- [ ] 未知意图 → 返回兜底提示

### D.5 回复格式

- [ ] 工人侧推荐格式符合 §10.5（编号+核心字段+小程序链接）
- [ ] 厂家/中介侧推荐格式符合 §10.5
- [ ] 工人侧不展示电话和详细地址
- [ ] 厂家/中介侧展示电话
- [ ] 每批展示 3 条（match.top_n）
- [ ] 底部有引导语

### D.6 图片消息

- [ ] 图片由 Worker 层下载并存入 uploads/（message_router 不直接调 wecom/client）
- [ ] message_router 通过 msg.image_url 获取已保存的图片 URL
- [ ] 上传流程中的图片关联到当前岗位/简历
- [ ] 非上传流程的图片回复"图片已收到，作为附件留存"

## E. 命令执行

### E.1 `/帮助`

- [ ] 发送"/帮助" → 返回帮助文案
- [ ] 发送"帮助" → 同上（同义词）
- [ ] 发送"怎么用" → 同上
- [ ] 帮助文案包含所有可用指令

### E.2 `/重新找`

- [ ] 有 session → 清空 criteria/snapshot/shown_items → 确认
- [ ] 无 session → "当前没有可清空的搜索条件"
- [ ] 发送"重来" → 同义词生效

### E.3 `/找岗位`

- [ ] 中介 → 切换到 search_job → 确认
- [ ] 非中介 → "只有中介账号可以切换双向模式"

### E.4 `/找工人`

- [ ] 中介 → 切换到 search_worker → 确认
- [ ] 非中介 → "只有中介账号可以切换双向模式"

### E.5 `/续期`

- [ ] 有 1 个岗位，无参数 → 续期 15 天 → 确认
- [ ] 有 1 个岗位，`/续期 30` → 续期 30 天
- [ ] 有多个岗位 → 返回列表
- [ ] 无岗位 → "未找到可续期的岗位"
- [ ] 发送"延期" → 同义词生效

### E.6 `/下架`

- [ ] 有在线岗位 → `delist_reason=manual_delist` → 确认
- [ ] 无在线岗位 → "未找到可下架的岗位"
- [ ] 发送"先不招了" → 同义词生效

### E.7 `/招满了`

- [ ] 有在线岗位 → `delist_reason=filled` → 确认
- [ ] 无在线岗位 → "未找到可操作的岗位"
- [ ] 发送"人招够了" → 同义词生效

### E.8 `/删除我的信息`

- [ ] 工人 → 简历软删除 + 对话日志软删除 + session 清空 + user.status=deleted → 确认
- [ ] 非工人 → 限制提示
- [ ] 发送"注销" → 同义词生效

### E.9 `/人工客服`

- [ ] 发送"/人工客服" → 返回人工客服联系方式引导文案
- [ ] 发送"转人工" → 同义词生效
- [ ] 发送"客服" → 同义词生效

### E.10 `/我的状态`

- [ ] 正常用户 → 返回账号状态 + 最近提交状态
- [ ] 发送"我被封了吗" → 同义词生效

## F. E2E 业务场景

### F.1 新工人完整流程

- [ ] 首次消息 → 注册 → 欢迎语
- [ ] 发送求职意向 → 推荐 Top 3
- [ ] 发送修正条件（follow_up） → 更新 criteria → 重新推荐
- [ ] 发送"更多" → 从快照取下一批
- [ ] 快照耗尽 → "已经是所有匹配结果了"
- [ ] `/重新找` → 清空 → 新一轮检索

### F.2 厂家发布岗位

- [ ] 预注册厂家首次消息 → 欢迎语
- [ ] 发送岗位描述（必填齐全）→ 入库 → 审核 → 确认
- [ ] 发送岗位描述（缺必填）→ 追问
- [ ] 补充后入库成功

### F.3 中介双向切换

- [ ] `/找岗位` → 切换 → 发送条件 → 检索岗位结果
- [ ] `/找工人` → 切换 → 发送条件 → 检索工人结果

### F.4 异常场景

- [ ] 企微消息重复投递 → 只处理一次
- [ ] LLM 超时 → 重试 → 死信 → "系统繁忙"
- [ ] 出站发送失败 → 重试 → 指数退避
- [ ] 超长文本输入 → 不崩溃

## G. 出站补偿

- [ ] 网络超时 → 立即重试 1 次
- [ ] API 限流 → 入 send_retry 队列 → 60s 后重试
- [ ] access_token 过期 → 自动刷新 → 重试成功
- [ ] 用户不存在 → 标记 inactive → 不重试
- [ ] 3 次重试仍失败 → 放弃 → 写 audit_log

## H. 对话日志

- [ ] 入站消息写入 conversation_log（direction="in"）
- [ ] 出站回复写入 conversation_log（direction="out"）
- [ ] **入站**消息 `wecom_msg_id` 不为空
- [ ] **出站**回复 `wecom_msg_id` 为 NULL（UNIQUE 约束要求）
- [ ] 一条入站 + 多条出站不会触发 UNIQUE 冲突
- [ ] `criteria_snapshot` 包含 prompt 版本
- [ ] 多条出站回复每条单独记录

## I. 部署与运维

- [ ] docker-compose.yml 中 worker 服务可正常启动
- [ ] docker-compose.prod.yml 中 worker 服务可正常启动
- [ ] Worker 进程独立于 app 进程（app 挂了 worker 继续消费）
- [ ] Worker 挂了 app 继续入队
- [ ] 队列状态可通过 Redis CLI 观测
- [ ] inbound_event 可通过 MySQL 查询观测

## J. 回归确认

- [ ] Phase 1~3 自动化测试仍然全部通过
- [ ] 健康检查端点 `/health` 仍然正常
- [ ] 数据库表结构无破坏性变更
- [ ] Redis 基础能力（session、锁、限流、幂等）未被 Phase 4 修改影响
