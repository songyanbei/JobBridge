# Phase 7 开发实施文档

> 基于：`collaboration/features/phase7-main.md`
> 面向角色：后端开发 + DevOps
> 状态：`draft`
> 创建日期：2026-04-18
> 最近修订：2026-04-18（按 codex 评审反馈修订：scheduler 同进程、audit_log 改走 loguru、user 删除时间使用 extra、nginx 共路径冲突、群消息独立重试队列、P95 口径、config key 命名）

## 1. 开发目标

本阶段开发目标，是为 JobBridge 补齐**定时任务体系**、**可观测能力**、**生产编排修复**、**备份恢复**与**上线验收**所需的所有能力，把 Phase 1~6 的代码收口成一个"可上线 + 可运维 + 可验收"的 MVP 系统。

开发时请始终记住：

- Phase 7 的职责是**收尾**，不是开新坑。任何新增业务功能、新增 admin 页面、二期能力的诉求一律拒绝
- 定时任务**在 app 进程内**通过 APScheduler `BackgroundScheduler` 启动；**不创建独立 scheduler 容器或进程**（对齐 `方案设计_v0.1.md:119 / :1098` 单进程部署基线）
- 所有任务必须**幂等 / 可重入 / 分批**；横向扩容通过 **Redis 分布式锁**保证单实例运行
- 所有运维事件走 **loguru 结构化日志**，**不扩展 `audit_log.action` 枚举**、**不新增 `audit_log.extra` 列**
- 所有告警必须**可去重**，不能把运营群刷屏
- 群日报推送失败走 **独立的 `queue:group_send_retry`**，不污染 Phase 4 `queue:send_retry`
- `/删除我的信息` 的 7 天计时起点写入 **`User.extra['deleted_at']`**（零 schema 变更），不依赖不存在的 `user.updated_at`
- nginx `/admin/*` SPA 与 Admin API **必须按 method + Accept 分流**
- `CORS_ORIGINS`、admin 默认密码、企微回调域名、HTTPS 证书等生产基线必须在上线前清零
- 备份恢复演练如果没做，视为 Phase 7 未完成

## 2. 当前代码现状

开工前请先确认：

- `backend/app/tasks/` 目录存在但仅有 `__init__.py`
- `backend/app/services/worker.py` 已实现 Worker 自心跳（每 60 秒写 `worker:heartbeat:{pid}` TTL=120s）、消费 `queue:send_retry` 的点对点消息补偿；**不消费群消息**
- `queue:send_retry` payload 是 `{userid, content, send_retry_count, backoff_until}`，消费者调用 `send_text(userid, content)`——群消息不兼容
- `backend/app/wecom/client.py` 有 `send_text()` / `download_media()`，**尚无** `send_text_to_group()`
- `backend/app/services/user_service.delete_user_data()` 已做立即软删 + 写 `audit_log(action='auto_pass', ...)`，但**没有记录删除时间**
- `backend/app/models.py:269-277` 中 `audit_log.action` 是封闭枚举 `auto_pass / auto_reject / manual_pass / manual_reject / manual_edit / undo / appeal / reinstate`；字段名为 `snapshot`（JSON），**无 `extra` 列**
- `backend/sql/seed.sql:53-55` 已有 `ttl.job.days` / `ttl.resume.days` / `ttl.conversation_log.days`
- `nginx/nginx.conf:7-10` 现有配置把 `/admin` 全部当静态处理，`POST /admin/login` 等 axios 请求会被 `try_files` 回退到 `/admin/index.html`（HTML），生产会失败
- `docker-compose.prod.yml` 当前含 5 个 service：`nginx + app + worker + mysql + redis`；Phase 7 **不新增 service**

如发现现状与上述不符，在 `collaboration/handoffs/` 中先记录差异再决定是否调整实施范围。

## 3. 开发原则

### 3.1 目录边界

- 本阶段只改 `backend/` + 根目录 `docker-compose.*`（仅在必要时）+ `nginx/` + `scripts/` + `.env.example`
- 不改 `frontend/`，除非 Phase 6 遗留阻塞上线的 P0 缺陷
- 不扩展 `audit_log.action` 枚举、不新增 `audit_log` 字段（如需审计能力增强，进入二期 Backlog）
- 不改 `方案设计_v0.1.md` / `docs/architecture.md` / `docs/implementation-plan.md`，除非项目负责人确认回写基线

### 3.2 依赖边界

- `tasks/*` 只做调度和业务编排，具体查询 / 更新通过 Phase 1~5 已有的 `services/*` 或 `models.py` 完成
- 允许在 `tasks/ttl_cleanup.py` 用 `sa.update()` / `sa.delete()` 做分批清理，但必须写明注释说明理由
- 调度任务只依赖 `db.py` / `core/redis_client.py` / `wecom/client.py` / `services/*`，不 import `api/*`

### 3.3 异常与幂等

- 每个任务必须独立处理异常，不允许把异常抛回 APScheduler 主循环（默认 `EVENT_JOB_ERROR` 已做兜底，但业务层仍需 `try/except` + loguru `exception`）
- 所有任务必须用 Redis 分布式锁（`task_lock:{task_name}`）保证单实例，锁 TTL 建议为任务预期最大耗时的 2x
- 不得在 `tasks/` 里写 `audit_log`；所有运维事件走 loguru

## 4. 开发顺序建议

1. `tasks/common.py`（分布式锁 + loguru helper）
2. `tasks/scheduler.py`（注册函数 `register_scheduler(app: FastAPI)`）
3. `app/main.py` lifespan 集成
4. `tasks/ttl_cleanup.py`
5. `services/user_service.delete_user_data()` 补 `User.extra['deleted_at']`
6. `tasks/worker_monitor.py`
7. `wecom/client.py` 新增 `send_text_to_group()` + `queue:group_send_retry` payload 约定
8. `tasks/send_retry_drain.py`
9. `tasks/daily_report.py`
10. LLM 调用日志打点
11. `nginx/nginx.conf` 改造
12. `.env.example` / `config.py` 校验
13. 备份脚本 + 恢复演练
14. 指标实测准备 + 7 天试运营
15. 上线前 Checklist + 验收报告

## 5. 模块实现指引

### 5.1 `tasks/common.py`

职责：

- 提供 `task_lock(name, ttl_seconds)` 上下文管理器（**带 owner token + Lua CAS 释放**，避免任务超时后误删他人锁）
- 提供 `log_event(event: str, **fields)` 统一打点

```python
import secrets
from contextlib import contextmanager
from typing import Generator
from loguru import logger
from app.core.redis_client import get_redis

# Lua 脚本：CAS 比较 value 后再删除，避免 TTL 过期后误删新 owner 的锁
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

@contextmanager
def task_lock(name: str, ttl: int = 3600) -> Generator[bool, None, None]:
    r = get_redis()
    key = f"task_lock:{name}"
    token = secrets.token_hex(16)
    acquired = bool(r.set(key, token, nx=True, ex=ttl))
    try:
        yield acquired
    finally:
        if acquired:
            try:
                r.eval(_RELEASE_SCRIPT, 1, key, token)
            except Exception:
                logger.exception(f"task_lock release failed: {name}")

def log_event(event: str, **fields) -> None:
    logger.bind(event=event, **fields).info(event)
```

**注意**：`ttl` 必须 ≥ 任务预期最大耗时的 2x（`ttl_cleanup` 建议 3600s，短任务建议 60~300s）。不要用任意短 TTL，否则任务运行中锁会被动过期。

调用示例：

```python
def run() -> None:
    with task_lock("ttl_cleanup", ttl=3600) as acquired:
        if not acquired:
            logger.info("ttl_cleanup: skipped, another instance holds the lock")
            return
        _run_ttl_cleanup()
```

### 5.2 `tasks/scheduler.py`

```python
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.config import settings
from app.tasks import ttl_cleanup, daily_report, worker_monitor, send_retry_drain

_scheduler: BackgroundScheduler | None = None

def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=settings.scheduler_timezone)

    sched.add_job(ttl_cleanup.run, CronTrigger.from_crontab("0 3 * * *"), id="ttl_cleanup", max_instances=1, coalesce=True)
    sched.add_job(daily_report.run, CronTrigger.from_crontab("0 9 * * *"), id="daily_report", max_instances=1, coalesce=True)
    sched.add_job(worker_monitor.check_heartbeat, IntervalTrigger(seconds=180), id="heartbeat", max_instances=1, coalesce=True)
    sched.add_job(worker_monitor.check_queue_backlog, IntervalTrigger(seconds=60), id="queue_backlog", max_instances=1, coalesce=True)
    sched.add_job(worker_monitor.check_dead_letter, IntervalTrigger(seconds=60), id="dead_letter", max_instances=1, coalesce=True)
    sched.add_job(send_retry_drain.check_backlog, IntervalTrigger(seconds=600), id="send_retry_check", max_instances=1, coalesce=True)
    sched.add_job(send_retry_drain.drain_group_send_retry, IntervalTrigger(seconds=30), id="group_send_drain", max_instances=1, coalesce=True)
    return sched

def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = build_scheduler()

    # 启动前：只打印 id 与 trigger（pending job 的 next_run_time 尚未计算，
    # APScheduler 3.x 在 start 前读取会抛 AttributeError）
    for job in _scheduler.get_jobs():
        logger.info(f"scheduler pending: id={job.id} trigger={job.trigger}")

    _scheduler.start()

    # 启动后：next_run_time 才可用
    for job in _scheduler.get_jobs():
        logger.info(f"scheduler running: id={job.id} next_run={job.next_run_time}")

def shutdown() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
```

注意：

- 必须使用 `BackgroundScheduler`，不要 `BlockingScheduler`（会阻塞 FastAPI 主线程）
- **先 `start()` 再读 `next_run_time`**：pending 状态的 job 尚未被调度器计算触发时间，启动前访问 `next_run_time` 会 `AttributeError`
- `max_instances=1 + coalesce=True` 防止同一 app 实例内任务堆叠
- 横向扩容多 app 实例时，分布式锁在各任务内部保证单实例执行（见 §5.1）

### 5.3 `app/main.py` 集成

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.tasks import scheduler as task_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    task_scheduler.start()
    try:
        yield
    finally:
        task_scheduler.shutdown()

app = FastAPI(lifespan=lifespan)
```

若现有 `main.py` 已有 lifespan，把上述逻辑整合进去；避免两套 lifespan。

### 5.4 `tasks/ttl_cleanup.py`

步骤顺序（建议按此顺序）：

1. 岗位过期软删：`UPDATE job SET delist_reason='expired', deleted_at=NOW() WHERE expires_at<NOW() AND delist_reason IS NULL AND deleted_at IS NULL`
2. 简历过期软删：`UPDATE resume SET deleted_at=NOW() WHERE expires_at<NOW() AND deleted_at IS NULL`
3. 岗位 7 天硬删（分批）
4. 简历 7 天硬删 + `storage.delete()` 附件
5. 用户主动删除 7 天硬删：扫 `user.status='deleted' AND JSON_EXTRACT(extra,'$.deleted_at') < NOW() - INTERVAL 7 DAY`，硬删其 `resume` / `conversation_log` 残留（`user` 记录保留）
6. `conversation_log.created_at < NOW() - INTERVAL 30 DAY` 硬删
7. `wecom_inbound_event.created_at < NOW() - INTERVAL 30 DAY` 硬删
8. `audit_log.created_at < NOW() - INTERVAL 180 DAY` 硬删

骨架：

```python
from loguru import logger
from sqlalchemy import text
from app.db import SessionLocal
from app.tasks.common import task_lock, log_event

BATCH_SIZE = 500

def run() -> None:
    with task_lock("ttl_cleanup", ttl=3600) as acquired:
        if not acquired:
            logger.info("ttl_cleanup: skipped, lock held")
            return
        stats = {}
        with SessionLocal() as db:
            stats["soft_delete_jobs"] = _soft_delete_expired_jobs(db)
            stats["soft_delete_resumes"] = _soft_delete_expired_resumes(db)
            stats["hard_delete_jobs"] = _batch_hard_delete(db, "job", "deleted_at < NOW() - INTERVAL 7 DAY")
            stats["hard_delete_resumes"] = _hard_delete_resumes_with_storage(db)
            stats["hard_delete_deleted_users"] = _hard_delete_deleted_users(db)
            stats["hard_delete_conversation"] = _batch_hard_delete(db, "conversation_log", "created_at < NOW() - INTERVAL 30 DAY")
            stats["hard_delete_inbound"] = _batch_hard_delete(db, "wecom_inbound_event", "created_at < NOW() - INTERVAL 30 DAY")
            stats["hard_delete_audit_log"] = _batch_hard_delete(db, "audit_log", "created_at < NOW() - INTERVAL 180 DAY")
        log_event("ttl_cleanup_summary", **stats)
```

每个 `_batch_hard_delete` 内部：

```python
def _batch_hard_delete(db, table: str, where: str) -> int:
    total = 0
    while True:
        r = db.execute(text(f"DELETE FROM {table} WHERE {where} LIMIT {BATCH_SIZE}"))
        db.commit()
        deleted = r.rowcount or 0
        total += deleted
        if deleted < BATCH_SIZE:
            break
    return total
```

注意事项：

- DELETE 前可加一次 `SELECT COUNT(*)` 打 loguru `debug` 便于核对，但不必每批都查
- 删除 `resume` 时先查 `images` JSON 列，逐个调 `storage.delete(key)`；ID 已汇总后批量 DELETE
- 每步 `try/except` 各自独立，异常写 loguru 后继续下一步

### 5.5 `services/user_service.delete_user_data()` 补丁

在函数末尾（现有 `return` 之前）添加：

```python
user = db.query(User).filter(User.external_userid == external_userid).one_or_none()
if user is not None:
    extra = dict(user.extra or {})
    # 存储为 MySQL 友好的 UTC 字符串（无 "T" 无时区偏移），便于 STR_TO_DATE 解析
    extra["deleted_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    user.extra = extra
```

变更点：

- `now` 已在函数开头定义（`datetime.now(timezone.utc)`）
- **存储格式**：固定为 `%Y-%m-%d %H:%M:%S`（UTC，无 `T`、无时区后缀），与 MySQL `STR_TO_DATE` 一次解析兼容；不使用 `isoformat()`（其输出 `2026-04-18T12:34:56+00:00`，MySQL 比较时需要额外 trim）
- `User.extra` 是 `MutableDict.as_mutable(sa.JSON)`；赋值新 dict 会触发变更跟踪
- 该补丁属 Phase 7 允许的"收口类修改"，不破坏 Phase 3/4 既定行为

对历史遗留 `status='deleted'` 但无 `extra.deleted_at` 的用户，`ttl_cleanup` 使用兜底查询；由于 `deleted_at` 已约定为 UTC，比较侧统一用 `UTC_TIMESTAMP()`，避免 MySQL 服务端时区不同导致偏差：

```sql
SELECT user.external_userid
FROM user
LEFT JOIN (
  SELECT target_id, MAX(created_at) AS last_delete_at
  FROM audit_log
  WHERE target_type='user' AND action='auto_pass'
    AND operator='system' AND reason LIKE '%/删除我的信息%'
  GROUP BY target_id
) al ON al.target_id = user.external_userid
WHERE user.status='deleted'
  AND COALESCE(
        STR_TO_DATE(
          JSON_UNQUOTE(JSON_EXTRACT(user.extra, '$.deleted_at')),
          '%Y-%m-%d %H:%i:%s'
        ),
        al.last_delete_at
      ) < UTC_TIMESTAMP() - INTERVAL 7 DAY
```

说明：

- `audit_log.created_at` 由 MySQL `NOW()` 默认生成，遵循 MySQL 服务器时区。生产部署建议 MySQL server timezone 设为 UTC（`default-time-zone='+00:00'`），使 `NOW() == UTC_TIMESTAMP()`；若服务器时区非 UTC，则兜底分支需要额外 `CONVERT_TZ` 处理
- **docker-compose.prod.yml MySQL 容器已建议配置 `TZ=UTC`**；如未配置，Phase 7 部署时需补

可选：Phase 7 提供一次性 backfill 脚本 `scripts/backfill_user_deleted_at.py`，把 audit_log 起点按 `%Y-%m-%d %H:%M:%S` 格式写回 `extra.deleted_at`。

### 5.6 `tasks/worker_monitor.py`

三个检查函数：

```python
def check_heartbeat() -> None:
    keys = list(get_redis().scan_iter("worker:heartbeat:*"))
    if not keys:
        _alert("worker_all_offline", "Worker 全部离线或心跳未上报")

def check_queue_backlog() -> None:
    n = get_redis().llen("queue:incoming")
    if n > settings.monitor_queue_incoming_threshold:
        _alert("queue_backlog", f"queue:incoming 积压 {n} 条，阈值 {settings.monitor_queue_incoming_threshold}")

def check_dead_letter() -> None:
    n = get_redis().llen("queue:dead_letter")
    if n > 0:
        _alert("dead_letter", f"queue:dead_letter 存在 {n} 条死信")
```

去重告警：

```python
def _alert(event: str, message: str) -> None:
    r = get_redis()
    dedupe_key = f"alert_dedupe:{event}"
    first_time = bool(r.set(dedupe_key, "1", nx=True, ex=settings.monitor_alert_dedupe_seconds))
    log_event(event, message=message, first_time=first_time)
    if first_time:
        _push_group(message)
```

推送：

```python
def _push_group(content: str) -> None:
    chat_id = settings.daily_report_chat_id
    if not chat_id:
        log_event("alert_push_skipped", reason="chat_id_empty")
        return
    ok = get_wecom_client().send_text_to_group(chat_id, content)
    if not ok:
        enqueue_group_send_retry(chat_id, content)
```

### 5.7 `wecom/client.py` 扩展

```python
def send_text_to_group(self, chat_id: str, content: str) -> bool:
    try:
        resp = self._request(
            "POST",
            f"{self.base_url}/cgi-bin/appchat/send",
            params={"access_token": self.get_token()},
            json={
                "chatid": chat_id,
                "msgtype": "text",
                "text": {"content": content},
                "safe": 0,
            },
        )
        errcode = resp.get("errcode", -1)
        if errcode == 0:
            return True
        logger.warning(f"send_text_to_group errcode={errcode} errmsg={resp.get('errmsg')}")
        return False
    except Exception as e:
        logger.exception(f"send_text_to_group failed: {e}")
        return False
```

`enqueue_group_send_retry`（放在 `tasks/send_retry_drain.py` 或 `core/redis_client.py`）：

```python
def enqueue_group_send_retry(chat_id: str, content: str, retry_count: int = 0, backoff: int = 60) -> None:
    payload = {
        "chat_id": chat_id,
        "content": content,
        "retry_count": retry_count,
        "backoff_until": time.time() + backoff,
    }
    get_redis().rpush("queue:group_send_retry", json.dumps(payload, ensure_ascii=False))
```

### 5.8 `tasks/send_retry_drain.py`

```python
SEND_RETRY_BACKOFFS = [60, 120, 300]
MAX_GROUP_RETRY = 3

def check_backlog() -> None:
    for queue in ("queue:send_retry", "queue:group_send_retry"):
        n = get_redis().llen(queue)
        if n > settings.monitor_send_retry_threshold:
            _alert("send_retry_backlog", f"{queue} 积压 {n} 条")

def drain_group_send_retry() -> None:
    with task_lock("group_send_drain", ttl=60) as acquired:
        if not acquired:
            return
        r = get_redis()
        raw = r.lpop("queue:group_send_retry")
        if not raw:
            return
        payload = json.loads(raw)
        now = time.time()
        if now < payload.get("backoff_until", 0):
            r.rpush("queue:group_send_retry", json.dumps(payload, ensure_ascii=False))
            return
        ok = get_wecom_client().send_text_to_group(payload["chat_id"], payload["content"])
        if ok:
            log_event("group_send_retry_success", retry_count=payload.get("retry_count", 0))
            return
        retry_count = payload.get("retry_count", 0) + 1
        if retry_count >= MAX_GROUP_RETRY:
            log_event("group_send_failed_final", retry_count=retry_count, chat_id=payload["chat_id"])
            return
        next_backoff = SEND_RETRY_BACKOFFS[min(retry_count - 1, len(SEND_RETRY_BACKOFFS) - 1)]
        payload["retry_count"] = retry_count
        payload["backoff_until"] = time.time() + next_backoff
        r.rpush("queue:group_send_retry", json.dumps(payload, ensure_ascii=False))
```

注意：

- **不消费 `queue:send_retry`**（Phase 4 Worker 已消费它）
- 每次 drain 只处理 1 条；APScheduler 间隔 30s 已够

### 5.9 `tasks/daily_report.py`

数据口径（统一走 `services/report_service.py`）：

| 指标 | 取值方式 |
|---|---|
| DAU | `report_service.get_dau(today_start)` |
| 今日上传 | `report_service.get_uploads(today_start)` |
| 今日检索次数 | `report_service.get_search_count(today_start)` |
| 命中率 / 空召回率 | `report_service.get_search_metrics(today_start)` |
| 审核打回率 | `report_service.get_audit_reject_rate(today_start)` |
| 待审积压 | `report_service.get_pending_count()` |
| 队列 / 死信 / Worker 健康 | Redis 直查（`LLEN queue:*` / `worker:heartbeat:*`） |
| 昨日对比 | `report_service.get_dau(yesterday_start)` 等 |

推送伪代码：

```python
def run() -> None:
    with task_lock("daily_report", ttl=600) as acquired:
        if not acquired:
            return
        content = _compose_report()
        chat_id = settings.daily_report_chat_id
        if not chat_id:
            log_event("daily_report_skipped_no_chat_id")
            return
        ok = get_wecom_client().send_text_to_group(chat_id, content)
        log_event("daily_report_generated", pushed=ok, length=len(content))
        if not ok:
            enqueue_group_send_retry(chat_id, content)
```

推送文案参考：

```
📊 JobBridge 日报 2026-04-20

DAU：432（↑ 12）
上传：岗位 28 / 简历 15
检索：1542 次（↑ 78）
命中率：82.4%（↓ 1.2%）
空召回率：18.2%
审核打回率：8.6%
新增封禁：1

⚙️ 运行
待审积压：12
死信数：0
Worker 健康：✅
```

不写 audit_log。

### 5.10 LLM 调用日志

在 `services/intent_service.py` / `services/search_service.py` 的 LLM 调用点包装：

```python
start = time.perf_counter()
try:
    result = extractor.extract(...)
    status = "ok"
    parse_failed = False
except JSONDecodeError:
    status = "parse_failed"
    parse_failed = True
    ...
except LLMTimeoutError:
    status = "timeout"
    ...
finally:
    log_event(
        "llm_call",
        provider=extractor.provider,
        model=extractor.model,
        prompt_version=extractor.prompt_version,
        input_tokens=getattr(result, "input_tokens", None),
        output_tokens=getattr(result, "output_tokens", None),
        duration_ms=int((time.perf_counter() - start) * 1000),
        intent=getattr(result, "intent", None),
        user_msg_id=msg_id,
        status=status,
    )
```

不重复写 `raw_response` 到日志（已在 `conversation_log.criteria_snapshot`）。

### 5.11 nginx 配置改造（P0-4 修复）

nginx 需要同时解决两个问题：

1. **`/admin/*` 同时承载 SPA 与 Admin API** → 按 `Accept` + method 分流，API 走 named location（避免 `if` 块里出现 `proxy_set_header`，该组合 `nginx -t` 可能通过但语义不可靠）
2. **SPA 静态资源路径** → Vite 默认 `base='/'` 打包的 `index.html` 引用 `/assets/xxx.js`，而 dist 被挂到 `/usr/share/nginx/html/admin`；浏览器访问 `/admin/login` 即使拿到 HTML，也会因 `/assets/*` 404 或被反代到 app 而白屏

#### 5.11.1 前端 Vite base（**前置条件**）

把 `frontend/vite.config.js` 的构建基路径设为 `/admin/`：

```js
export default defineConfig({
  plugins: [vue()],
  base: '/admin/',
  server: { ... },
})
```

重新 `npm run build`，检查 `frontend/dist/index.html` 中 `<script>` / `<link>` 的 `src / href` 前缀为 `/admin/assets/...`。此后所有静态资源都落在 `/admin/*` 路径下，可被 `location /admin/` 的 `try_files` 正确命中。

> 这是 Phase 6 输出物的一处补齐，Phase 7 允许收口；如 Phase 6 已设置 `base='/admin/'` 则本步跳过，只需在测试中复核。

#### 5.11.2 完整 `nginx/nginx.conf`

```nginx
map $http_accept $admin_is_api {
    default              0;
    "~*application/json" 1;
}

server {
    listen 80;
    server_name _;
    charset utf-8;
    client_max_body_size 10m;

    gzip on;
    gzip_types application/json application/javascript text/css text/plain;

    location = /nginx-health {
        return 200 "ok";
        add_header Content-Type text/plain;
        access_log off;
    }

    # /admin/* —— SPA + Admin API 共存
    # 规则：Accept: application/json 或非 GET/HEAD → 走 @admin_api；其余 → 静态 + SPA fallback
    location /admin/ {
        # 在 if 里只使用安全 directive（set / return / rewrite），
        # 通过 error_page + recursive_error_pages 跳转到 named location，
        # 避免在 if 块里放 proxy_pass / proxy_set_header（nginx 官方不推荐）
        error_page 418 = @admin_api;
        recursive_error_pages on;

        if ($request_method !~ ^(GET|HEAD)$) { return 418; }
        if ($admin_is_api = 1)               { return 418; }

        root /usr/share/nginx/html;
        try_files $uri $uri/ /admin/index.html;
    }

    # /admin 根路径兜底（省略尾斜杠时跳到登录页）
    location = /admin {
        return 302 /admin/;
    }

    location @admin_api {
        proxy_pass http://app:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # 企微 webhook / 小程序事件回传 / health
    location / {
        proxy_pass http://app:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_send_timeout 10s;
    }
}
```

#### 5.11.3 关键点

- `location /admin/` 末尾必须带 `/`，配合 `root /usr/share/nginx/html` 使 `/admin/foo.js` 映射到 `/usr/share/nginx/html/admin/foo.js`（挂载点和请求路径一致）
- `if` 块里**只用** `return 418`；所有 `proxy_pass / proxy_set_header / proxy_read_timeout` 放在 `@admin_api` named location 中，避免 `if + proxy directives` 在 nginx 中语义不可靠
- `error_page 418 = @admin_api` + `recursive_error_pages on` 把 API 请求内部跳到 named location，`-t` 可通过且语义明确
- `location = /admin { return 302 /admin/; }` 处理"用户输入 `/admin` 不带尾斜杠"的情况
- 部署前必须在 nginx 容器里跑 `nginx -t` 验证
- 前端 axios 必须显式 `Accept: application/json`（Phase 6 已满足）
- 如上线后发现特定 UA 的 `Accept` 组合匹配错误，降级方案：回落到"白名单方法 + 已知 API 路径正则"；严重时把 Admin API 迁到 `/api/admin/*` 并回写架构文档（回滚级变更）
- 生产 HTTPS 证书、`listen 443 ssl;`、`ssl_certificate*` 由运维按 §14.5 完善

### 5.12 `.env.example` 与 `config.py`

`.env.example` 新增：

```
APP_ENV=production
CORS_ORIGINS=https://admin.jobbridge.example.com

SCHEDULER_TIMEZONE=Asia/Shanghai

# 日报与告警推送群（可留空，为空时跳过推送只写 loguru）
DAILY_REPORT_CHAT_ID=

# 监控阈值（走 .env，不走 admin 配置页）
MONITOR_QUEUE_INCOMING_THRESHOLD=50
MONITOR_SEND_RETRY_THRESHOLD=20
MONITOR_ALERT_DEDUPE_SECONDS=600
```

`config.py` 在 `APP_ENV=production` 时启动校验：

```python
from pydantic import model_validator

class Settings(...):
    app_env: str = "development"
    cors_origins: str = ""
    scheduler_timezone: str = "Asia/Shanghai"
    daily_report_chat_id: str = ""
    monitor_queue_incoming_threshold: int = 50
    monitor_send_retry_threshold: int = 20
    monitor_alert_dedupe_seconds: int = 600

    @model_validator(mode="after")
    def _check_production(self):
        if self.app_env == "production":
            # CORS_ORIGINS 支持逗号多值；逐个 origin 检查，任一为 "*" 即拒绝
            origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
            if not origins or any(o == "*" for o in origins):
                raise ValueError(
                    "CORS_ORIGINS must be concrete origins in production "
                    "(no empty value, no '*' in any comma-separated entry)"
                )
        return self
```

校验覆盖三种非法配置：`CORS_ORIGINS=""` / `CORS_ORIGINS="*"` / `CORS_ORIGINS="https://a.com, *"`（混入 `*`）。

### 5.13 备份与恢复

`scripts/backup_mysql.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail
TS=$(date +%Y%m%d_%H%M%S)
OUT_DIR=${BACKUP_DIR:-/data/jobbridge/backup/mysql}
mkdir -p "$OUT_DIR"
docker exec jobbridge-mysql \
  mysqldump -u root -p"${MYSQL_ROOT_PASSWORD:?}" jobbridge \
  | gzip > "$OUT_DIR/jobbridge_${TS}.sql.gz"
find "$OUT_DIR" -type f -mtime +14 -delete
```

`scripts/backup_uploads.sh`：打包 `uploads/` 为 `tar.gz` 并保留 14 天。

`scripts/restore_drill.sh`：读取指定 `.sql.gz` 导回预发 MySQL；首行注释 `# 禁止在生产环境执行`。

Crontab：

```
30 3 * * * /opt/jobbridge/scripts/backup_mysql.sh   >> /var/log/jobbridge/backup.log 2>&1
0 4 * * *  /opt/jobbridge/scripts/backup_uploads.sh >> /var/log/jobbridge/backup.log 2>&1
```

恢复演练流程：

1. 选一份最近 7 天内的备份
2. 在预发 MySQL 执行 `zcat xxx.sql.gz | mysql ...`
3. 启动预发 app（定时任务会随 app 启动）
4. 用一个标记用户在生产的数据（如最近一条 job）在预发环境验证可读
5. 记录演练耗时、数据条数、异常
6. 把结果写入 `phase7-release-report.md`

### 5.14 指标实测口径

#### 5.14.1 P95 回复延迟

当前代码事实（影响口径选型）：

- `wecom_inbound_event` **只有 `created_at`**（`models.py:446`），**没有 `received_at` 字段**；`created_at` 即回调到达时间
- `conversation_log` 出站记录的 `wecom_msg_id` **固定为 NULL**（`services/worker.py:631`，UNIQUE 约束要求），**无法按 `wecom_msg_id` 关联入站与出站**
- Phase 4 Worker 对同一 `userid` 使用 `session_lock:{userid}` 分布式锁，**保证同一用户消息串行处理**

由此确定两种口径：

**口径 A：`userid + 时序窗口`近似关联（零改动，默认）**

- 起点：`wecom_inbound_event.created_at`
- 终点：同一 `userid` 下首条 `conversation_log(direction='out', created_at >= in.created_at)` 的 `created_at`
- 正确性保证：Phase 4 `session_lock:{userid}` 保证同一 userid 消息顺序执行，首条 out 必然对应刚处理完的 in
- 误差：
  - `send_text` 返回成功到企微实际送达之间的网络时延（一般 < 1s）
  - 若 Worker 对同一条 in 回复多条 out，只取第一条
- 示例 SQL（可放在 `scripts/phase7_indicator_snapshot.py`）：

```sql
WITH pairs AS (
  SELECT
    ie.id,
    ie.msg_id,
    ie.from_userid,
    ie.created_at AS in_at,
    (
      SELECT MIN(cl.created_at)
      FROM conversation_log cl
      WHERE cl.userid = ie.from_userid
        AND cl.direction = 'out'
        AND cl.created_at >= ie.created_at
    ) AS out_at
  FROM wecom_inbound_event ie
  WHERE ie.created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY
    AND ie.status = 'done'
)
SELECT TIMESTAMPDIFF(MICROSECOND, in_at, out_at) / 1000 AS latency_ms
FROM pairs
WHERE out_at IS NOT NULL
ORDER BY latency_ms;
-- 应用侧取第 95 百分位
```

**口径 B：`criteria_snapshot.source_msg_id` 精确关联（可选增强）**

如客户对口径严格，Phase 7 允许在 `services/worker.py` 写出站 `conversation_log` 时把入站 `msg_id` 附加到 `criteria_snapshot`：

```python
# worker.py 写出站 ConversationLog 时
criteria_snapshot = {
    **(reply.criteria_snapshot or {}),
    "source_msg_id": msg.msg_id,
}
```

对应 SQL：

```sql
JOIN conversation_log cl
  ON cl.direction = 'out'
 AND JSON_UNQUOTE(JSON_EXTRACT(cl.criteria_snapshot, '$.source_msg_id')) = ie.msg_id
```

**选型原则**：默认走口径 A；如客户对 P95 精度有强诉求，实施口径 B（约 1~2 小时改动 + 观测）。两种口径在 `phase7-release-report.md` 中必须明确标注所选。

#### 5.14.2 其它 6 项指标

- 结构化提取成功率：`conversation_log` 中 `intent` 非空 / 非空总数（剔除 `intent='chitchat'` 或 `status='parse_failed'` 视具体口径）
- 检索成功率：search_* intent 下返回 `ranked_count > 0` 的比例
- 空召回率：返回 0 候选的比例
- 审核打回率：当日 `audit_log` 中 `action IN ('manual_reject','auto_reject')` 占当日审核总量
- 死信率：`wecom_inbound_event.status='dead_letter'` 数量 / 当日 `received` 消息数
- 删除请求完成率：`User.extra['deleted_at']` 写入 7 天后用户 `resume` / `conversation_log` 已硬删的比例

每日把指标写一份 Markdown 到 `phase7-release-report.md` 的"7 天试运营"章节。

## 6. 开发自测清单

- [ ] `docker compose -f docker-compose.prod.yml up -d` **5 容器**全部 healthy
- [ ] `docker exec jobbridge-nginx nginx -t` 通过
- [ ] 启动日志同时出现 `scheduler pending: id=...` 和 `scheduler running: id=... next_run=...`
- [ ] 手工触发 `ttl_cleanup.run()`（通过 Python shell 或临时 admin endpoint）条数正确
- [ ] 手工触发 `daily_report.run()`，未配置 `chat_id` 时只 loguru 不报错
- [ ] 杀掉 worker 3 分钟内运营群收到告警
- [ ] `RPUSH queue:dead_letter "mock"` → 1 分钟内告警
- [ ] 连续 3 次触发同一告警 → 10 分钟内推送 1 次
- [ ] 发送一条 `/删除我的信息` → `user.extra->$.deleted_at` 存在，格式 `YYYY-MM-DD HH:MM:SS`（不带 `T`）
- [ ] 将该用户 `extra.deleted_at` 改为 8 天前 → 再跑一次 ttl_cleanup → 简历 / 对话被硬删
- [ ] `curl -H "Accept: application/json" http://localhost/admin/me` 返回 JSON（401 也算成功，证明走到 API）
- [ ] 浏览器访问 `http://localhost/admin/login` 返回 HTML
- [ ] 浏览器 DevTools 加载 `/admin/login` 时，`/admin/assets/*.js` / `*.css` 请求均 200（Vite `base='/admin/'` 生效）
- [ ] 访问 `/admin`（不带尾斜杠）被 302 到 `/admin/`
- [ ] `scripts/backup_mysql.sh` 生成 `.sql.gz`
- [ ] `scripts/restore_drill.sh` 在预发恢复成功
- [ ] `APP_ENV=production` + `CORS_ORIGINS=*` 启动即报错
- [ ] `APP_ENV=production` + `CORS_ORIGINS=https://a.com,*` 启动即报错（逐个 origin 校验）
- [ ] `APP_ENV=production` + `CORS_ORIGINS=` 启动即报错
- [ ] `send_text_to_group()` 推送日报模板到测试群收到
- [ ] `RPUSH queue:group_send_retry ...` 能被 30s 内 drain 消费
- [ ] `RPUSH queue:send_retry ...`（老队列）仍由 Phase 4 Worker 消费，本阶段任务不动它
- [ ] task_lock 在任务超时后不误删新 owner 的锁（通过 TTL 小于任务耗时的单测验证）

## 7. Handoff 规则

如果发现以下情况，写入 `collaboration/handoffs/backend-to-frontend.md` 或 `frontend-to-backend.md`：

- Phase 5 报表接口缺少 Phase 7 日报所需字段
- 前端 Phase 6 遗留 P0 缺陷阻塞上线验收
- `Accept` header 在真实浏览器某些场景下命中错误（触发 nginx 降级策略讨论）
- `CORS_ORIGINS` 生产域名与前端访问入口不一致
- `wecom_msg_id` 与 `conversation_log` 的关联查询性能在 7 天数据量下 > 1s（考虑是否加索引）

形成结论后回填到 `phase7-main.md`。

## 8. 注意事项

1. **不要把 scheduler 分出独立进程**（对齐方案设计单进程基线）
2. **不要扩展 `audit_log.action` 枚举**，运维事件走 loguru
3. **不要在 tasks 中 import `api/*`**，避免循环依赖
4. **不要把群日报塞进 `queue:send_retry`**，使用独立 `queue:group_send_retry`
5. **不要把监控阈值暴露到 admin 配置页**，对齐 §13.3
6. **不要硬编码 `user.updated_at`**（字段不存在），统一走 `User.extra['deleted_at']`
7. **TTL 硬删必须分批 + 幂等**，每步独立 commit
8. **nginx 的 SPA/API 分流必须覆盖 Phase 6 所有路径**，上线前用 axios + 浏览器两侧回归
9. **生产 `CORS_ORIGINS` 绝不能 `*` 或空**
10. **不要跳过 §17.1.1 的 7 天试运营**直接标注指标达标
11. **备份脚本不能把 `.env` 或 `admin_user` 哈希写进日志或备份文件名**
12. **不要在 Phase 7 插入二期需求**，所有新需求走变更控制进 Backlog
