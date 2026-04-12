# 消息基础契约说明

> 状态：locked
> 创建日期：2026-04-12
> 面向阶段：Phase 4 (webhook / worker)
> 关联代码：`backend/app/core/redis_client.py`、`backend/app/models.py` (WecomInboundEvent)

## 1. Redis 队列 Key 约定

| Key 名称 | 用途 | 数据结构 |
|---|---|---|
| `queue:incoming` | 待处理消息队列 | Redis List (RPUSH 入队，BLPOP 消费) |
| `queue:dead_letter` | 死信队列（不可恢复或超过重试上限） | Redis List (RPUSH 入队) |

## 2. Webhook 入口调用顺序

webhook 路由收到企微回调后，按以下顺序调用 `redis_client` 方法：

```
1. check_rate_limit(userid, window=10, max_count=5)
   - 返回 False → 被限流，直接返回空响应，不入队
   - 返回 True → 继续

2. check_msg_duplicate(msg_id)
   - 返回 True → 消息重复，直接返回空响应，不重复入队
   - 返回 False → 非重复，继续

3. enqueue_message(json.dumps(payload))
   - payload 至少包含: msg_id, from_user, msg_type, content, media_id, create_time
   - 推入 queue:incoming
```

关键约束：
- 被限流的消息**不入队**，不写 `wecom_inbound_event`
- 重复消息**不重复入队**，不重复写 `wecom_inbound_event`
- 正常消息入队后，应同步写入 `wecom_inbound_event` 记录，状态为 `received`

## 3. Worker 消费顺序

worker 从 `queue:incoming` 消费消息，流程如下：

```
1. dequeue_message(timeout=5)
   - 返回 None → 无消息，继续等待
   - 返回 message_json → 解析并处理

2. 更新 wecom_inbound_event 状态为 processing

3. 执行业务处理（意图识别 → 字段抽取 → 检索/上传 → 回复）

4. 处理成功 → 更新状态为 done

5. 处理失败 →
   a. retry_count < 2 → 更新状态为 failed，重新入队 queue:incoming
   b. retry_count >= 2 → 更新状态为 dead_letter，入队 queue:dead_letter
   c. 明确不可恢复错误 → 直接更新状态为 dead_letter，入队 queue:dead_letter
```

## 4. wecom_inbound_event 状态流转

```
              ┌─────────┐
              │ received │  ← webhook 入队时写入
              └────┬─────┘
                   │ worker 取到消息
              ┌────▼──────┐
              │ processing │
              └────┬───────┘
                   │
          ┌────────┼────────┐
          │        │        │
     ┌────▼──┐ ┌───▼───┐ ┌──▼────────┐
     │  done │ │ failed │ │ dead_letter│
     └───────┘ └───┬───┘ └───────────┘
                   │ 重新入队后再次消费
              ┌────▼──────┐
              │ processing │
              └────┬───────┘
                   │
          ┌────────┼────────┐
          │        │        │
     ┌────▼──┐         ┌───▼────────┐
     │  done │         │ dead_letter │ ← 第 2 次重试仍失败
     └───────┘         └────────────┘
```

### 状态值说明

| 状态 | 含义 |
|---|---|
| `received` | 消息已入队，等待 worker 消费 |
| `processing` | worker 正在处理 |
| `done` | 处理成功完成 |
| `failed` | 本次处理失败，但仍可重试 |
| `dead_letter` | 达到重试上限或明确不可恢复 |

### 关联 ORM 字段

对应 `WecomInboundEvent` 模型（`backend/app/models.py`）：
- `status`: 上述 5 个状态值
- `retry_count`: 当前重试次数
- `error_message`: 最近一次失败的错误信息
- `worker_started_at`: worker 开始处理的时间
- `worker_finished_at`: worker 完成/失败的时间

## 5. 死信转移规则

- 处理失败后最多重试 **2 次**
- 第 2 次重试后仍失败 → 转入 `queue:dead_letter`，状态更新为 `dead_letter`
- 明确不可恢复的错误（如消息格式非法、用户已删除等）可直接进入 `dead_letter`，无需重试

## 6. Redis Key 命名汇总

| Key 模式 | 用途 | TTL |
|---|---|---|
| `session:{userid}` | 用户会话状态 | 30 分钟 |
| `msg:{msg_id}` | 消息幂等去重 | 10 分钟 |
| `rate:{userid}` | 用户限流计数 | 10 秒（滑动窗口） |
| `lock:{userid}` | 用户消息串行锁 | 30 秒 |
| `queue:incoming` | 待处理消息队列 | 无（持久化） |
| `queue:dead_letter` | 死信队列 | 无（持久化） |
