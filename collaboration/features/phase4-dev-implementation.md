# Phase 4 开发实施文档

> 基于：`collaboration/features/phase4-main.md`
> 面向角色：后端开发
> 状态：`draft`
> 创建日期：2026-04-14

## 1. 开发目标

本阶段开发目标，是把"企微消息进来 → 异步处理 → 回复出去"这条核心链路彻底打通。

开发时请始终记住：

- Phase 3 已经把所有业务 service 做好了（user / intent / conversation / audit / search / upload / permission），Phase 4 不要重写这些逻辑
- Phase 4 的核心交付是三个新文件：`webhook.py`、`worker.py`、`message_router.py`
- webhook 只做"验签 + 幂等 + 限流 + 入队 + 快速返回"，绝不同步调业务逻辑
- Worker 是独立进程，不是后台线程，不是 APScheduler 任务
- message_router 是编排层，调用已有 service，不在 router 中重写业务判断

## 2. 当前代码现状

当前仓库已具备：

- `app/wecom/*`：验签、加解密、XML 解析、消息发送客户端（Phase 2 交付）
- `app/services/*`：7 个业务 service（Phase 3 交付）
- `app/core/redis_client.py`：session、锁、限流、幂等、队列基础方法
- `app/llm/prompts.py`：业务版 prompt 已定稿
- `app/models.py`、`app/schemas/*`：ORM 和 DTO 已完成
- `app/config.py`：含企微配置项

当前缺失：

- `app/api/webhook.py`
- `app/services/worker.py`
- `app/services/message_router.py`
- `app/api/` 下路由注册到 `main.py`
- docker-compose 中 worker 服务
- 命令路由和完整命令执行（Phase 3 仅预留了 service 基座）

## 3. 开发原则

### 3.1 依赖边界

- `webhook.py` 只依赖 `wecom/crypto`、`wecom/callback`、`core/redis_client`、`models`（写 inbound_event）
- `webhook.py` 绝对不能 import `message_router` 或任何 service
- `worker.py` 依赖 `core/redis_client`（队列消费）、`message_router`（业务处理）、`wecom/client`（发送回复）、`models`（更新状态）
- `message_router.py` 依赖 Phase 3 的所有 service，但**不直接依赖 `wecom/*`**（回复发送和图片下载都交给 Worker 层）
- `message_router.py` 返回 `list[ReplyMessage]`，不负责发送，不负责图片下载

### 3.2 异步边界

- webhook → Redis 队列 → Worker → message_router：这条链路是异步的
- message_router 内部的 service 调用是同步的（符合 Phase 1 决策：不上 async）
- Worker 进程内用同步阻塞式消费（BLPOP），不需要 asyncio

### 3.3 错误隔离

- webhook 层的错误不能影响返回 200（即使写 inbound_event 失败也要返回 200）
- worker 层的错误不能导致消息丢失（异常消息重入队列或进死信）
- message_router 层的错误应被 worker 捕获并记录，不能导致 worker 进程崩溃

## 4. 逐模块开发指引

### 4.1 模块 A：`api/webhook.py`

文件：`backend/app/api/webhook.py`

#### 4.1.1 路由定义

```python
from fastapi import APIRouter, Request, Query
router = APIRouter()

@router.get("/webhook/wecom")
async def verify_callback(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...)
):
    """企微回调 URL 验证"""
    # 1. verify_signature(token, timestamp, nonce, echostr)
    # 2. decrypt_message(echostr) → 明文
    # 3. 返回明文

@router.post("/webhook/wecom")
async def receive_callback(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...)
):
    """企微消息回调"""
    # 1. 读取 body (XML)
    # 2. 验签
    # 3. 解密
    # 4. 解析消息
    # 5. 幂等检查
    # 6. 限流检查
    # 7. 写入 inbound_event
    # 8. 入队
    # 9. 返回 200
```

#### 4.1.2 注册到 main.py

```python
# main.py
from app.api.webhook import router as webhook_router
app.include_router(webhook_router)
```

#### 4.1.3 幂等检查实现

```python
def check_idempotent(msg_id: str) -> bool:
    """返回 True 表示该消息未处理过，False 表示重复。"""
    r = get_redis()
    try:
        result = r.set(f"msg:{msg_id}", "1", nx=True, ex=600)
        return result is not None
    except RedisError:
        # Redis 不可用，降级为 DB 检查
        # 后续写入 inbound_event 时靠 UNIQUE 约束兜底
        return True
```

#### 4.1.4 限流检查实现

复用 `core/redis_client.py` 已有的 `check_rate_limit()` 方法。限流参数从 `system_config` 读取并缓存（建议缓存 60 秒，避免每次请求查 DB）。

被限流时：
1. 不写入 `wecom_inbound_event`
2. 不入队
3. 异步发一条限流提示（可用 `threading.Thread` 或忽略，避免阻塞返回）
4. 返回 200

#### 4.1.5 入队消息格式

```python
import json

queue_msg = {
    "msg_id": msg.msg_id,
    "from_userid": msg.from_userid,
    "msg_type": msg.msg_type,
    "content": msg.content,
    "media_id": msg.media_id,  # 仅图片消息
    "create_time": msg.create_time,
    "inbound_event_id": event.id  # wecom_inbound_event 表主键
}
redis.rpush("queue:incoming", json.dumps(queue_msg))
```

#### 4.1.6 异常兜底

- 验签失败 → 返回 403
- 解密失败 → 记录 error log，返回 200（防止企微重试）
- 写入 inbound_event 失败 → 记录 error log，仍然入队（队列消费时重建 event）
- 入队失败（Redis 异常）→ 记录 error log，返回 200（消息通过 inbound_event 表可恢复）

### 4.2 模块 B：`services/worker.py`

文件：`backend/app/services/worker.py`

#### 4.2.1 进程入口

```python
import signal
import threading
import os
import json
import logging
import time

logger = logging.getLogger(__name__)

class Worker:
    def __init__(self):
        self.running = True
        self.pid = os.getpid()

    def start(self):
        """启动 Worker"""
        self._setup_signal_handlers()
        self._start_heartbeat()
        self._startup_recovery()
        self._main_loop()

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False

    def _start_heartbeat(self):
        """心跳线程"""
        def heartbeat():
            while self.running:
                try:
                    redis.set(f"worker:heartbeat:{self.pid}", "1", ex=120)
                except Exception:
                    logger.warning("Heartbeat write failed")
                time.sleep(60)
        t = threading.Thread(target=heartbeat, daemon=True)
        t.start()

    def _startup_recovery(self):
        """启动自检：恢复 processing 状态的消息"""
        # 查询 wecom_inbound_event 中 status=processing 的记录
        # 将这些消息重新入队
        pass

    def _main_loop(self):
        while self.running:
            # BLPOP 阻塞消费，超时 5 秒
            msg_data = redis.blpop("queue:incoming", timeout=5)
            if msg_data is None:
                # 超时无消息，检查 send_retry 队列
                self._process_send_retry()
                continue
            self._process_message(json.loads(msg_data[1]))

def main():
    worker = Worker()
    worker.start()

if __name__ == "__main__":
    main()
```

#### 4.2.2 消息处理核心

```python
def _process_message(self, msg_data: dict):
    inbound_event_id = msg_data.get("inbound_event_id")
    userid = msg_data["from_userid"]

    try:
        # 1. 获取 userid 分布式锁
        lock_acquired = redis.set(
            f"session_lock:{userid}", "1", nx=True, ex=30
        )
        if not lock_acquired:
            # 该用户有消息正在处理，延迟重入队列
            time.sleep(1)
            redis.rpush("queue:incoming", json.dumps(msg_data))
            return

        # 2. 更新 inbound_event → processing
        self._update_event_status(inbound_event_id, "processing")

        # 3. 构造 WeComMessage 对象
        msg = self._build_message(msg_data)

        # 3.5 如果是图片消息，Worker 负责下载并存储
        if msg.msg_type == "image" and msg.media_id:
            from app.wecom.client import WeComClient
            from app.storage.base import get_storage
            client = WeComClient()
            image_data = client.download_media(msg.media_id)
            storage = get_storage()
            image_url = storage.save(
                image_data, f"images/{userid}/{msg.msg_id}.jpg"
            )
            msg.image_url = image_url  # 附加到消息对象供 router 使用

        # 4. 调用 message_router
        from app.services.message_router import process
        replies = process(msg)

        # 5. 发送回复
        for reply in replies:
            self._send_reply(reply)

        # 6. 写入 conversation_log
        self._write_conversation_log(msg, replies)

        # 7. 更新 inbound_event → done
        self._update_event_status(inbound_event_id, "done")

    except Exception as e:
        self._handle_error(msg_data, inbound_event_id, e)

    finally:
        # 释放 userid 锁
        redis.delete(f"session_lock:{userid}")
```

#### 4.2.3 错误处理与重试

```python
def _handle_error(self, msg_data, inbound_event_id, error):
    retry_count = msg_data.get("_retry_count", 0)

    if retry_count < 2:
        # 重入队列
        msg_data["_retry_count"] = retry_count + 1
        redis.rpush("queue:incoming", json.dumps(msg_data))
        self._update_event_status(
            inbound_event_id, "failed",
            error_message=str(error),
            retry_count=retry_count + 1
        )
    else:
        # 进死信
        redis.rpush("queue:dead_letter", json.dumps(msg_data))
        self._update_event_status(
            inbound_event_id, "dead_letter",
            error_message=str(error),
            retry_count=retry_count + 1
        )
        # 尝试发送兜底回复
        try:
            client = WeComClient()
            client.send_text(
                msg_data["from_userid"],
                "系统繁忙，请稍后再试"
            )
        except Exception:
            logger.error("Dead letter fallback reply failed")
```

#### 4.2.4 出站重试队列消费

```python
def _process_send_retry(self):
    """低优先级消费 queue:send_retry"""
    msg_data = redis.lpop("queue:send_retry")
    if not msg_data:
        return

    retry_msg = json.loads(msg_data)
    backoff_until = retry_msg.get("backoff_until", 0)

    if time.time() < backoff_until:
        # 还没到退避时间，放回队列
        redis.rpush("queue:send_retry", json.dumps(retry_msg))
        return

    try:
        client = WeComClient()
        client.send_text(retry_msg["userid"], retry_msg["content"])
    except Exception as e:
        send_retry_count = retry_msg.get("send_retry_count", 0)
        if send_retry_count < 3:
            # 指数退避重入
            backoff_seconds = [60, 120, 300][min(send_retry_count, 2)]
            retry_msg["send_retry_count"] = send_retry_count + 1
            retry_msg["backoff_until"] = time.time() + backoff_seconds
            redis.rpush("queue:send_retry", json.dumps(retry_msg))
        else:
            # 放弃，写 audit_log
            self._write_audit_log("send_failed", retry_msg, str(e))
```

### 4.3 模块 C：`services/message_router.py`

文件：`backend/app/services/message_router.py`

#### 4.3.1 入口方法

```python
from dataclasses import dataclass
from typing import List

@dataclass
class ReplyMessage:
    userid: str
    content: str
    msg_type: str = "text"

def process(msg: WeComMessage) -> List[ReplyMessage]:
    """
    消息路由主入口。
    接收解析后的企微消息，返回待发送的回复列表。
    """
    # 1. 用户识别
    user_info = user_service.identify_or_register(msg.from_userid)

    # 2. 状态拦截（使用已有 check_user_status()）
    block_msg = user_service.check_user_status(user_info)
    if block_msg is not None:
        return [ReplyMessage(msg.from_userid, block_msg)]

    # 3. 更新活跃时间
    user_service.update_last_active(msg.from_userid, db)

    # 4. 按消息类型分流
    if msg.msg_type == "text":
        return _handle_text(msg, user_info)
    elif msg.msg_type == "image":
        return _handle_image(msg, user_info)
    elif msg.msg_type == "voice":
        return [ReplyMessage(msg.from_userid, VOICE_NOT_SUPPORTED)]
    elif msg.msg_type in ("file", "video"):
        return [ReplyMessage(msg.from_userid, FILE_NOT_SUPPORTED)]
    elif msg.msg_type == "event":
        logger.info(f"Received event: {msg.content}")
        return []
    else:
        return [ReplyMessage(msg.from_userid, UNKNOWN_TYPE_REPLY)]
```

#### 4.3.2 文本处理链路

```python
def _handle_text(msg, user_info) -> List[ReplyMessage]:
    userid = msg.from_userid
    content = msg.content.strip()

    # 1. 首次欢迎判定（优先于意图分类）
    if user_info.should_welcome:
        return [ReplyMessage(userid, _build_welcome(user_info))]

    # 2. 读取 session
    session = conversation_service.load_session(userid)

    # 3. 统一意图分类（内部已含 显式命令 → show_more → LLM 三级优先）
    #    注意：classify_intent() 签名为 (text, role, history, current_criteria)
    intent_result = intent_service.classify_intent(
        text=content,
        role=user_info.role,
        history=session.history if session else None,
        current_criteria=session.search_criteria if session else None,
    )

    # 4. 按意图分发
    intent = intent_result.intent

    if intent == "command":
        cmd = intent_result.structured_data.get("command")
        return _handle_command(cmd, msg, user_info, session)
    elif intent in ("upload_job", "upload_resume"):
        return _handle_upload(msg, user_info, intent_result, session)
    elif intent in ("search_job", "search_worker"):
        return _handle_search(msg, user_info, intent_result, session)
    elif intent == "upload_and_search":
        return _handle_upload_and_search(msg, user_info, intent_result, session)
    elif intent == "follow_up":
        return _handle_follow_up(msg, user_info, intent_result, session)
    elif intent == "show_more":
        return _handle_show_more(msg, user_info, session)
    elif intent == "chitchat":
        return _handle_chitchat(msg, user_info)
    else:
        return [ReplyMessage(userid, FALLBACK_REPLY)]
```

#### 4.3.3 各意图处理函数要点

**`_handle_upload()`**：
- 调用 `upload_service.process_upload(content, user_info, intent_result)`
- 如果有必填字段缺失 → 返回追问文案
- 如果入库成功 → 返回确认文案
- 如果审核拦截 → 返回拦截提示

**`_handle_search()`**：
- 调用 `conversation_service.merge_criteria_patch(session, intent_result.criteria_patch)` 更新 criteria
- 检查必填字段是否齐全
- 不齐全 → 追问
- 齐全 → 调用 `search_service.search_jobs(criteria, raw_query, session, user_ctx, db)` 或 `search_workers(...)`
- `search_service` 内部已包含权限过滤和格式化，返回 `SearchResult`
- 调用 `conversation_service.record_shown(session, shown_ids)` 记录已展示

**`_handle_follow_up()`**：
- 调用 `conversation_service.merge_criteria_patch(session, intent_result.criteria_patch)`
- 如果 criteria 有效变更（返回 True）→ 快照自动清空 → 重新调用 `search_service.search_jobs()` / `search_workers()`
- 否则 → 继续用已有快照

**`_handle_show_more()`**：
- 调用 `search_service.show_more(session, user_ctx, db)`
- 返回 `SearchResult`（内部已完成权限过滤和格式化）

**`_handle_chitchat()`**：
- 返回引导语

#### 4.3.4 回复文案常量

建议在 `message_router.py` 顶部或独立文件定义所有固定文案：

```python
BLOCKED_REPLY = "您的账号已被限制使用，如有疑问请联系客服 xxx"
DELETED_REPLY = "账号已进入删除状态，请联系客服处理"
VOICE_NOT_SUPPORTED = "暂不支持语音，请发送文字"
FILE_NOT_SUPPORTED = "暂不支持文件，请直接用文字描述"
RATE_LIMITED_REPLY = "您发送太频繁了，请稍后再试"
SYSTEM_BUSY_REPLY = "系统繁忙，请稍后再试"
HUMAN_AGENT_REPLY = "已为您转人工客服，请稍候；也可直接联系 xxx。"
FALLBACK_REPLY = "抱歉，我没有理解您的意思。您可以直接告诉我您想找什么工作，或输入 /帮助 查看使用指南。"
```

### 4.4 模块 D：命令执行器

#### 4.4.1 命令路由

```python
COMMAND_MAP = {
    "help": "help",
    "reset_search": "reset_search",
    "switch_to_job": "switch_search_job",
    "switch_to_worker": "switch_search_worker",
    "renew_job": "renew_job",
    "delist_job": "delist_job",
    "filled_job": "filled_job",
    "delete_my_data": "delete_user_data",
    "human_agent": "human_agent",
    "my_status": "my_status",
}
# 注意：命令字符串到归并 key 的映射已在 intent_service._COMMAND_MAP 中定义，
# classify_intent() 返回 intent="command" + structured_data={"command": "help"} 等。
# 此处 COMMAND_MAP 是 归并 key → handler 函数名 的映射。

def _handle_command(cmd: str, msg, user_info) -> List[ReplyMessage]:
    handler_name = COMMAND_MAP.get(cmd)
    handler = getattr(_command_handlers, handler_name, None)
    if handler:
        return handler(msg, user_info)
    return [ReplyMessage(msg.from_userid, FALLBACK_REPLY)]
```

#### 4.4.2 各命令实现要点

**`/帮助`**：返回固定帮助文案，包含可用指令和示例。

**`/重新找`**：
- 读取 session，如果有 criteria → 清空 → 回复确认
- 没有 criteria → 回复"当前没有可清空的搜索条件"

**`/找岗位` / `/找工人`**：
- 检查 `user_info.role == "broker"`
- 不是中介 → 回复"只有中介账号可以切换双向模式"
- 是中介 → 更新 session 方向 → 回复确认

**`/续期`**：
- 解析参数（默认 15 天，支持 `/续期 15` `/续期 30`）
- 查询用户名下未过期岗位
- 无岗位 → 回复"未找到可续期的岗位"
- 1 个 → 直接续期
- 多个 → 返回列表让用户选择

**`/下架`**：
- 查询用户名下在线岗位
- 无 → 回复异常文案
- 有 → 更新 `delist_reason=manual_delist` → 回复确认

**`/招满了`**：
- 同 `/下架`，`delist_reason=filled`

**`/删除我的信息`**：
- 检查 `user_info.role == "worker"`
- 调用已有 service 执行：软删除简历 + 对话日志 + 清空 session + 更新 user.status=deleted
- 回复确认

**`/人工客服`**：
- 返回人工客服联系方式引导文案
- 无前置条件、无角色限制

**`/我的状态`**：
- 调用 `user_service.get_user_status(external_userid, db)`
- 返回状态摘要

### 4.5 模块 E：图片消息处理

```python
def _handle_image(msg, user_info) -> List[ReplyMessage]:
    """图片消息处理。
    
    注意：图片下载和存储由 Worker 层完成（Worker 调用 WeComClient.download_media()
    并存入 storage），message_router 不直接依赖 wecom/client。
    Worker 将已保存的图片 URL 附加到 msg 对象后再传入本方法。
    """
    userid = msg.from_userid
    image_url = msg.image_url  # Worker 层已下载并保存，此处为 storage URL

    if not image_url:
        return [ReplyMessage(userid, "图片处理失败，请稍后重试。")]

    # 检查是否在上传流程中
    session = conversation_service.load_session(userid)
    if session and session.current_intent in ("upload_job", "upload_resume"):
        # 关联到当前岗位/简历
        # 如 upload_service 尚无 attach_image() 方法，Phase 4 需新增
        # upload_service.attach_image(userid, image_url, db)
        return [ReplyMessage(userid, "图片已收到，将作为附件保留。请继续发送文字信息。")]

    return [ReplyMessage(userid, "图片已收到。目前仅支持文字描述发布信息，图片作为附件留存。")]
```

### 4.6 模块 F：部署配置

#### 4.6.1 docker-compose.yml（开发环境）

在已有配置基础上增加 worker 服务：

```yaml
worker:
  build: ./backend
  command: python -m app.services.worker
  depends_on:
    - mysql
    - redis
  env_file: .env
  restart: unless-stopped
  volumes:
    - ./backend:/app
    - uploads_data:/app/uploads
```

#### 4.6.2 docker-compose.prod.yml

同上，但不挂载源码 volume。

#### 4.6.3 main.py 路由注册

```python
from app.api.webhook import router as webhook_router
app.include_router(webhook_router)
```

### 4.7 模块 G：对话日志写入

由 Worker 负责，在消息处理完成后写入：

```python
def _write_conversation_log(self, msg, replies):
    db = get_db()

    # 入站消息 — 写入 wecom_msg_id（UNIQUE 约束）
    inbound_log = ConversationLog(
        userid=msg.from_userid,
        direction="in",
        msg_type=msg.msg_type,
        content=msg.content,
        wecom_msg_id=msg.msg_id,  # 仅入站写此字段
        created_at=datetime.now()
    )
    db.add(inbound_log)

    # 出站回复 — wecom_msg_id 留 NULL（因为 UNIQUE 约束，多条出站不能写同一个值）
    for reply in replies:
        outbound_log = ConversationLog(
            userid=reply.userid,
            direction="out",
            msg_type="text",
            content=reply.content,
            wecom_msg_id=None,  # 出站不写，避免 UNIQUE 冲突
            created_at=datetime.now()
        )
        db.add(outbound_log)

    db.commit()
```

### 4.8 模块 H：出站发送与失败补偿

```python
def _send_reply(self, reply: ReplyMessage):
    """发送一条回复，失败时入 send_retry 队列"""
    client = WeComClient()
    try:
        client.send_text(reply.userid, reply.content)
    except TokenExpiredError:
        # token 过期自动刷新后重试
        client.refresh_token()
        client.send_text(reply.userid, reply.content)
    except RateLimitError:
        # API 限流，入重试队列
        self._enqueue_send_retry(reply, backoff=60)
    except UserNotFoundError:
        # 用户不存在/已退出，不重试
        user_service.mark_inactive(reply.userid)
        logger.warning(f"User {reply.userid} not found, marked inactive")
    except Exception as e:
        # 其他网络错误
        try:
            client.send_text(reply.userid, reply.content)  # 立即重试 1 次
        except Exception:
            self._enqueue_send_retry(reply, backoff=60)

def _enqueue_send_retry(self, reply, backoff):
    retry_msg = {
        "userid": reply.userid,
        "content": reply.content,
        "send_retry_count": 0,
        "backoff_until": time.time() + backoff
    }
    redis.rpush("queue:send_retry", json.dumps(retry_msg))
```

## 5. 开发顺序建议

建议按以下顺序开发和联调：

1. **webhook.py**（先保证消息能收进来）
   - 实现 GET 验证端点
   - 实现 POST 消息接收
   - 注册到 main.py
   - 用 curl 或 Postman 模拟企微回调验证
2. **worker.py 骨架**（先保证消费链路通）
   - 实现主循环和队列消费
   - 暂时只做日志输出，确认消费正常
3. **message_router.py**（核心编排）
   - 先实现消息类型分流
   - 再接入文本主链路（意图分发）
   - 最后接入命令路由
4. **命令执行器**（逐个实现和测试）
5. **出站补偿和心跳**（收尾）
6. **docker-compose 配置**（整体联调）

## 6. 测试辅助

### 6.1 本地模拟企微回调

不依赖真实企微环境，可编写测试脚本模拟回调：

```python
# scripts/simulate_wecom.py
import requests
from app.wecom.crypto import encrypt_message, generate_signature

def send_test_message(content, from_userid="test_worker_001"):
    """模拟企微发送文本消息"""
    xml = f"""<xml>
        <MsgId>test_{int(time.time())}</MsgId>
        <FromUserName>{from_userid}</FromUserName>
        <MsgType>text</MsgType>
        <Content>{content}</Content>
        <CreateTime>{int(time.time())}</CreateTime>
    </xml>"""

    encrypted = encrypt_message(xml)
    timestamp = str(int(time.time()))
    nonce = "test_nonce"
    signature = generate_signature(token, timestamp, nonce, encrypted)

    resp = requests.post(
        f"http://localhost:8000/webhook/wecom"
        f"?msg_signature={signature}&timestamp={timestamp}&nonce={nonce}",
        data=encrypted_xml_body
    )
    print(f"Status: {resp.status_code}, Body: {resp.text}")
```

### 6.2 队列状态检查

```bash
# 查看队列长度
redis-cli LLEN queue:incoming
redis-cli LLEN queue:dead_letter
redis-cli LLEN queue:send_retry

# 查看 Worker 心跳
redis-cli KEYS "worker:heartbeat:*"

# 查看 inbound_event 状态分布
mysql -e "SELECT status, COUNT(*) FROM wecom_inbound_event GROUP BY status"
```

## 7. 注意事项

1. **不要在 webhook 中做任何慢操作**：验签、幂等、入队都应在毫秒级完成
2. **不要吞掉异常**：所有错误必须记录日志，出现 dead_letter 必须可追溯
3. **不要跳过 userid 分布式锁**：并发消息不加锁会导致 criteria 覆盖丢失
4. **不要让 message_router 直接发送消息**：router 只返回 `ReplyMessage`，发送由 worker 负责
5. **不要硬编码限流参数**：必须从 `system_config` 读取
6. **不要忘记写 `wecom_msg_id`**：conversation_log 必须关联企微消息 ID
7. **注意 Worker 进程的 DB session 管理**：Worker 是长时间运行的进程，每次消息处理应使用独立的 DB session，处理完毕后 commit 并 close，避免连接泄漏
