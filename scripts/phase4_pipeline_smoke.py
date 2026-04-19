"""Phase 4 端到端链路 smoke 脚本（真实 MySQL + Redis）。

覆盖：
- 基础：Redis 幂等 / 限流 / 分布式锁
- Webhook 落库入队（绕过验签/解密，直接按已解密消息构造）
- Worker 同步处理一次消息（手工驱动 _process_message）
- 命令链路：/帮助 / /我的状态
- 新工人自动注册 + 欢迎语
- 消息类型分流：voice 拒绝 / image 留存
- 对话日志：入站含 wecom_msg_id、出站为 NULL
- P0-2：image/file 恢复时 media_id + 原始 msg_type 回填
- P1-1：'续约一下那个岗位' 不被误识别为 renew_job
- P1-2：限流提示 push 到 queue:rate_limit_notify + 60s 去重
- P1-3：invalidate_token 公开方法生效
- P1-4：broker 无方向时 LLM search_job 被尊重并回写 session
- P1-5：file/video/link/location 原始 msg_type 保留
- 幂等 L2：相同 msg_id 第二次走 DB UNIQUE 兜底
- Worker 异常重试 → 死信 + 兜底回复

运行：
    cd /mnt/d/work/JobBridge/backend
    source .venv-wsl/bin/activate
    PYTHONPATH=. python /mnt/d/work/JobBridge/scripts/phase4_pipeline_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

RESULTS: list[tuple[str, bool, str]] = []


@contextmanager
def scenario(name: str):
    print(f"\n> {name}")
    try:
        yield
        RESULTS.append((name, True, ""))
        print(f"  [PASS]")
    except AssertionError as e:
        RESULTS.append((name, False, f"AssertionError: {e}"))
        print(f"  [FAIL] {e}")
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  [ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()


def setup():
    from sqlalchemy import text as sql_text
    from app.core.redis_client import (
        QUEUE_DEAD_LETTER, QUEUE_INCOMING, QUEUE_RATE_LIMIT_NOTIFY,
        QUEUE_SEND_RETRY, get_redis,
    )
    from app.db import SessionLocal

    print("=== Phase 4 Pipeline Smoke (Real MySQL + Redis) ===\n")

    r = get_redis()
    for pat in ("msg:*", "rate:*", "session:*", "lock:*",
                "rate_limit_notified:*", "worker:heartbeat:*"):
        for k in r.scan_iter(pat):
            r.delete(k)
    for q in (QUEUE_INCOMING, QUEUE_DEAD_LETTER,
              QUEUE_SEND_RETRY, QUEUE_RATE_LIMIT_NOTIFY):
        r.delete(q)

    db = SessionLocal()
    try:
        db.execute(sql_text("DELETE FROM conversation_log WHERE userid LIKE 'test_e2e_%'"))
        db.execute(sql_text("DELETE FROM wecom_inbound_event WHERE from_userid LIKE 'test_e2e_%'"))
        db.execute(sql_text(
            "DELETE FROM audit_log WHERE target_id LIKE 'test_e2e_%' "
            "OR operator LIKE 'test_e2e_%'"
        ))
        db.execute(sql_text("DELETE FROM job WHERE owner_userid LIKE 'test_e2e_%'"))
        db.execute(sql_text("DELETE FROM resume WHERE owner_userid LIKE 'test_e2e_%'"))
        db.execute(sql_text("DELETE FROM user WHERE external_userid LIKE 'test_e2e_%'"))
        db.commit()
    finally:
        db.close()


def test_redis_primitives():
    from app.core.redis_client import (
        check_msg_duplicate, check_rate_limit, user_lock,
    )

    with scenario("Redis idempotency: second check returns True"):
        msg_id = f"e2e_dedup_{int(time.time()*1000)}"
        assert check_msg_duplicate(msg_id) is False
        assert check_msg_duplicate(msg_id) is True

    with scenario("Redis rate limit: 6th in 10s returns False"):
        uid = f"test_e2e_rl_{int(time.time()*1000)}"
        results = [check_rate_limit(uid, window=10, max_count=5) for _ in range(6)]
        assert results[:5] == [True] * 5
        assert results[5] is False

    with scenario("Redis user_lock: same userid exclusive"):
        uid = f"test_e2e_lock_{int(time.time()*1000)}"
        with user_lock(uid, timeout=1) as acq1:
            assert acq1 is True
            with user_lock(uid, timeout=1) as acq2:
                assert acq2 is False


def test_webhook_insert_and_enqueue():
    from app.api.webhook import _insert_inbound_event
    from app.core.redis_client import QUEUE_INCOMING, enqueue_message, get_redis
    from app.db import SessionLocal
    from app.models import WecomInboundEvent
    from app.wecom.callback import WeComMessage

    with scenario("Webhook: text msg writes inbound_event + enqueues"):
        msg = WeComMessage(
            msg_id=f"e2e_txt_{int(time.time()*1000)}",
            from_user="test_e2e_worker_1", msg_type="text",
            content="suzhou find factory", create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)
        assert event_id is not None

        db = SessionLocal()
        try:
            row = db.query(WecomInboundEvent).filter_by(id=event_id).first()
            assert row.status == "received"
            assert row.msg_type == "text"
            assert row.media_id is None
        finally:
            db.close()

        payload = {
            "msg_id": msg.msg_id, "from_userid": msg.from_user,
            "msg_type": msg.msg_type, "content": msg.content,
            "media_id": msg.media_id, "create_time": msg.create_time,
            "inbound_event_id": event_id,
        }
        enqueue_message(json.dumps(payload), QUEUE_INCOMING)
        assert get_redis().llen(QUEUE_INCOMING) >= 1

    with scenario("Webhook: image msg saves media_id in dedicated column (P0-2)"):
        msg = WeComMessage(
            msg_id=f"e2e_img_{int(time.time()*1000)}",
            from_user="test_e2e_worker_2", msg_type="image",
            media_id="MEDIA_ABC_123", create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)
        db = SessionLocal()
        try:
            row = db.query(WecomInboundEvent).filter_by(id=event_id).first()
            assert row.msg_type == "image"
            assert row.media_id == "MEDIA_ABC_123", f"got {row.media_id}"
        finally:
            db.close()

    with scenario("Webhook: file msg keeps raw msg_type not coerced to event (P1-5)"):
        msg = WeComMessage(
            msg_id=f"e2e_file_{int(time.time()*1000)}",
            from_user="test_e2e_worker_3", msg_type="file",
            media_id="MEDIA_FILE_XYZ", create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)
        db = SessionLocal()
        try:
            row = db.query(WecomInboundEvent).filter_by(id=event_id).first()
            assert row.msg_type == "file", f"got {row.msg_type}"
            assert row.media_id == "MEDIA_FILE_XYZ"
        finally:
            db.close()

    with scenario("Webhook: unknown msg_type falls back to 'other'"):
        msg = WeComMessage(
            msg_id=f"e2e_unk_{int(time.time()*1000)}",
            from_user="test_e2e_worker_4", msg_type="some_new_type",
            create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)
        db = SessionLocal()
        try:
            row = db.query(WecomInboundEvent).filter_by(id=event_id).first()
            assert row.msg_type == "other", f"got {row.msg_type}"
        finally:
            db.close()


def test_worker_help_command_roundtrip():
    from sqlalchemy import text as sql_text
    from app.api.webhook import _insert_inbound_event
    from app.db import SessionLocal
    from app.models import ConversationLog, User, WecomInboundEvent
    from app.services.command_service import HELP_TEXT
    from app.services.conversation_service import clear_session
    from app.services.worker import Worker
    from app.wecom.callback import WeComMessage

    with scenario("Worker: brand-new worker /help -> auto register + welcome"):
        uid = "test_e2e_worker_help"
        clear_session(uid)

        msg = WeComMessage(
            msg_id=f"e2e_help_{int(time.time()*1000)}",
            from_user=uid, msg_type="text", content="/帮助",
            create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)

        sent = []
        with patch("app.services.worker.WeComClient") as MockClient:
            client_inst = MagicMock()
            client_inst.send_text.side_effect = (
                lambda u, c: sent.append((u, c)) or {"errcode": 0}
            )
            MockClient.return_value = client_inst
            Worker()._process_message({
                "msg_id": msg.msg_id, "from_userid": uid,
                "msg_type": "text", "content": "/帮助", "media_id": "",
                "create_time": msg.create_time, "inbound_event_id": event_id,
            })

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(external_userid=uid).first()
            assert user is not None and user.role == "worker"
            # worker 首次欢迎发生在此次消息处理过程中，
            # update_last_active 已被调用，标记为非首次
            db.execute(sql_text(
                "UPDATE user SET last_active_at=NOW() WHERE external_userid=:u"
            ), {"u": uid})
            db.commit()
        finally:
            db.close()

        assert len(sent) == 1, f"expect 1 reply, got {len(sent)}"
        assert "JobBridge" in sent[0][1], f"expect welcome, got {sent[0][1][:40]!r}"

        db = SessionLocal()
        try:
            row = db.query(WecomInboundEvent).filter_by(id=event_id).first()
            assert row.status == "done", f"status={row.status}"
            assert row.worker_started_at is not None
            assert row.worker_finished_at is not None

            logs = db.query(ConversationLog).filter_by(userid=uid).all()
            in_logs = [l for l in logs if l.direction == "in"]
            out_logs = [l for l in logs if l.direction == "out"]
            assert len(in_logs) == 1
            assert len(out_logs) == 1
            assert in_logs[0].wecom_msg_id == msg.msg_id
            assert out_logs[0].wecom_msg_id is None, "outbound wecom_msg_id must be NULL"
        finally:
            db.close()

    with scenario("Worker: returning worker /help -> HELP_TEXT"):
        uid = "test_e2e_worker_help"
        msg = WeComMessage(
            msg_id=f"e2e_help2_{int(time.time()*1000)}",
            from_user=uid, msg_type="text", content="/帮助",
            create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)
        sent = []
        with patch("app.services.worker.WeComClient") as MockClient:
            client_inst = MagicMock()
            client_inst.send_text.side_effect = (
                lambda u, c: sent.append((u, c)) or {"errcode": 0}
            )
            MockClient.return_value = client_inst
            Worker()._process_message({
                "msg_id": msg.msg_id, "from_userid": uid, "msg_type": "text",
                "content": "/帮助", "media_id": "",
                "create_time": msg.create_time, "inbound_event_id": event_id,
            })
        assert len(sent) == 1
        assert sent[0][1] == HELP_TEXT, f"expect HELP_TEXT, got {sent[0][1][:40]!r}"


def test_worker_voice_file_image():
    from app.api.webhook import _insert_inbound_event
    from app.db import SessionLocal
    from app.models import User
    from app.services.message_router import (
        FILE_NOT_SUPPORTED, IMAGE_RECEIVED_NON_UPLOAD, VOICE_NOT_SUPPORTED,
    )
    from app.services.worker import Worker
    from app.wecom.callback import WeComMessage

    uid = "test_e2e_nonfirst_worker"
    db = SessionLocal()
    try:
        if db.query(User).filter_by(external_userid=uid).first() is None:
            db.add(User(
                external_userid=uid, role="worker", status="active",
                can_search_jobs=True, can_search_workers=False,
                last_active_at=datetime.now(timezone.utc),
            ))
            db.commit()
    finally:
        db.close()

    def _run(msg_type: str, media_id: str, content: str):
        msg = WeComMessage(
            msg_id=f"e2e_{msg_type}_{int(time.time()*1000)}",
            from_user=uid, msg_type=msg_type, media_id=media_id, content=content,
            create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)
        sent = []
        with patch("app.services.worker.WeComClient") as MockClient:
            client_inst = MagicMock()
            client_inst.download_media.return_value = b"\x89PNG\r\n\x1a\nfake"
            client_inst.send_text.side_effect = (
                lambda u, c: sent.append((u, c)) or {"errcode": 0}
            )
            MockClient.return_value = client_inst
            Worker()._process_message({
                "msg_id": msg.msg_id, "from_userid": uid, "msg_type": msg_type,
                "content": content, "media_id": media_id,
                "create_time": msg.create_time, "inbound_event_id": event_id,
            })
        return sent

    with scenario("Worker: voice -> VOICE_NOT_SUPPORTED"):
        sent = _run("voice", "VOICE_XYZ", "")
        assert len(sent) == 1 and sent[0][1] == VOICE_NOT_SUPPORTED

    with scenario("Worker: file -> FILE_NOT_SUPPORTED"):
        sent = _run("file", "FILE_123", "")
        assert len(sent) == 1 and sent[0][1] == FILE_NOT_SUPPORTED

    with scenario("Worker: image non-upload -> IMAGE_RECEIVED_NON_UPLOAD"):
        sent = _run("image", "IMG_NOUP", "")
        assert len(sent) == 1 and sent[0][1] == IMAGE_RECEIVED_NON_UPLOAD


def test_p0_2_startup_recovery_preserves_media_id():
    from app.core.redis_client import QUEUE_INCOMING, get_redis
    from app.db import SessionLocal
    from app.models import WecomInboundEvent
    from app.services.worker import Worker

    with scenario("P0-2: startup_recovery preserves media_id + image msg_type"):
        db = SessionLocal()
        try:
            zombie = WecomInboundEvent(
                msg_id=f"e2e_zombie_img_{int(time.time()*1000)}",
                from_userid="test_e2e_zombie",
                msg_type="image", media_id="ZOMBIE_MEDIA_999",
                content_brief="[image] media_id saved",
                status="processing", retry_count=0,
            )
            db.add(zombie); db.commit(); db.refresh(zombie)
            zombie_id = zombie.id
            zombie_msg_id = zombie.msg_id
        finally:
            db.close()

        r = get_redis()
        r.delete(QUEUE_INCOMING)

        with patch("app.services.worker.WeComClient"):
            Worker()._startup_recovery()

        queued = r.lpop(QUEUE_INCOMING)
        assert queued is not None, "recovery should requeue"
        payload = json.loads(queued)
        assert payload["msg_id"] == zombie_msg_id
        assert payload["msg_type"] == "image", f"got {payload['msg_type']}"
        assert payload["media_id"] == "ZOMBIE_MEDIA_999", f"got {payload['media_id']}"
        assert payload["content"] == "", f"media content should be empty, got {payload['content']!r}"
        assert payload["_recovered"] is True

        db = SessionLocal()
        try:
            row = db.query(WecomInboundEvent).filter_by(id=zombie_id).first()
            assert row.status == "received", f"got {row.status}"
        finally:
            db.close()


def test_p1_1_xu_prefix_strict():
    from app.services.intent_service import _match_command

    with scenario("P1-1: loose '续' prefix requires digit (续约/续保/续杯 -> None)"):
        assert _match_command("续约一下那个岗位") is None
        assert _match_command("续保") is None
        assert _match_command("续杯") is None
        assert _match_command("续15天") == ("renew_job", "15天")


def test_p1_2_rate_limit_notify_dedicated_queue():
    from app.api.webhook import _async_rate_limit_notify
    from app.core.redis_client import (
        QUEUE_RATE_LIMIT_NOTIFY, QUEUE_SEND_RETRY, get_redis,
    )

    r = get_redis()

    with scenario("P1-2: rate-limit notify goes to dedicated queue"):
        uid = f"test_e2e_rl_notify_{int(time.time()*1000)}"
        r.delete(f"rate_limit_notified:{uid}")
        r.delete(QUEUE_RATE_LIMIT_NOTIFY); r.delete(QUEUE_SEND_RETRY)

        _async_rate_limit_notify(uid)
        assert r.llen(QUEUE_RATE_LIMIT_NOTIFY) == 1, "should enter dedicated queue"
        assert r.llen(QUEUE_SEND_RETRY) == 0, "must not pollute send_retry"
        payload = json.loads(r.lindex(QUEUE_RATE_LIMIT_NOTIFY, 0))
        assert payload["userid"] == uid
        assert payload["source"] == "rate_limit_notify"

    with scenario("P1-2: 60s dedup prevents duplicate push"):
        uid = f"test_e2e_rl_dedup_{int(time.time()*1000)}"
        r.delete(f"rate_limit_notified:{uid}")
        r.delete(QUEUE_RATE_LIMIT_NOTIFY)

        _async_rate_limit_notify(uid)
        _async_rate_limit_notify(uid)
        _async_rate_limit_notify(uid)
        assert r.llen(QUEUE_RATE_LIMIT_NOTIFY) == 1, "should push only once in 60s"
        ttl = r.ttl(f"rate_limit_notified:{uid}")
        assert 0 < ttl <= 60, f"dedup key TTL should be (0, 60], got {ttl}"


def test_p1_3_invalidate_token():
    from app.wecom.client import WeComClient

    with scenario("P1-3: invalidate_token clears cache under lock"):
        c = WeComClient(corp_id="c", secret="s", agent_id="1000001")
        c._access_token = "abc"
        c._token_expires_at = 9_999_999_999
        c.invalidate_token()
        assert c._access_token == ""
        assert c._token_expires_at == 0


def test_p1_4_broker_search_direction():
    from app.schemas.conversation import SessionState
    from app.services.message_router import _resolve_search_direction
    from app.services.user_service import UserContext

    with scenario("P1-4: broker + intent=search_job -> search_job + session write"):
        ctx = UserContext(
            external_userid="b1", role="broker", status="active",
            display_name=None, company=None, contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        sess = SessionState(role="broker", broker_direction=None)
        d = _resolve_search_direction("search_job", ctx, sess)
        assert d == "search_job"
        assert sess.broker_direction == "search_job"

    with scenario("P1-4: broker + intent=search_worker -> search_worker + session write"):
        ctx = UserContext(
            external_userid="b2", role="broker", status="active",
            display_name=None, company=None, contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        sess = SessionState(role="broker", broker_direction=None)
        d = _resolve_search_direction("search_worker", ctx, sess)
        assert d == "search_worker"
        assert sess.broker_direction == "search_worker"


def test_my_status_command():
    from app.api.webhook import _insert_inbound_event
    from app.db import SessionLocal
    from app.models import User
    from app.services.worker import Worker
    from app.wecom.callback import WeComMessage

    uid = "test_e2e_factory_status"
    db = SessionLocal()
    try:
        if db.query(User).filter_by(external_userid=uid).first() is None:
            db.add(User(
                external_userid=uid, role="factory", status="active",
                company="test_factory", contact_person="zhang",
                phone="13800000000",
                can_search_jobs=False, can_search_workers=True,
                last_active_at=datetime.now(timezone.utc),
            ))
            db.commit()
    finally:
        db.close()

    with scenario("/我的状态: factory returns status summary"):
        msg = WeComMessage(
            msg_id=f"e2e_status_{int(time.time()*1000)}",
            from_user=uid, msg_type="text", content="/我的状态",
            create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)
        sent = []
        with patch("app.services.worker.WeComClient") as MockClient:
            client_inst = MagicMock()
            client_inst.send_text.side_effect = (
                lambda u, c: sent.append((u, c)) or {"errcode": 0}
            )
            MockClient.return_value = client_inst
            Worker()._process_message({
                "msg_id": msg.msg_id, "from_userid": uid, "msg_type": "text",
                "content": "/我的状态", "media_id": "",
                "create_time": msg.create_time, "inbound_event_id": event_id,
            })
        assert len(sent) == 1
        body = sent[0][1]
        assert "账号状态" in body and "正常" in body
        assert "厂家" in body


def test_idempotency_l2_db_unique():
    from app.api.webhook import _insert_inbound_event
    from app.db import SessionLocal
    from app.models import WecomInboundEvent
    from app.wecom.callback import WeComMessage

    with scenario("L2 idempotency: same msg_id second insert returns existing id"):
        msg = WeComMessage(
            msg_id=f"e2e_l2_{int(time.time()*1000)}",
            from_user="test_e2e_l2_user", msg_type="text", content="hi",
            create_time=int(time.time()),
        )
        first = _insert_inbound_event(msg)
        second = _insert_inbound_event(msg)
        assert first is not None and second is not None
        assert first == second, f"first={first} second={second}"

        db = SessionLocal()
        try:
            cnt = db.query(WecomInboundEvent).filter_by(msg_id=msg.msg_id).count()
            assert cnt == 1, f"expected 1, got {cnt}"
        finally:
            db.close()


def test_worker_retry_and_dead_letter():
    from app.api.webhook import _insert_inbound_event
    from app.core.redis_client import (
        QUEUE_DEAD_LETTER, QUEUE_INCOMING, get_redis,
    )
    from app.db import SessionLocal
    from app.models import User, WecomInboundEvent
    from app.services.worker import MAX_RETRY, Worker
    from app.wecom.callback import WeComMessage

    uid = "test_e2e_retry_user"
    db = SessionLocal()
    try:
        if db.query(User).filter_by(external_userid=uid).first() is None:
            db.add(User(
                external_userid=uid, role="worker", status="active",
                can_search_jobs=True, can_search_workers=False,
                last_active_at=datetime.now(timezone.utc),
            ))
            db.commit()
    finally:
        db.close()

    with scenario("Worker dead_letter: MAX_RETRY=2 failures -> dead_letter + fallback reply"):
        r = get_redis()
        r.delete(QUEUE_INCOMING); r.delete(QUEUE_DEAD_LETTER)

        msg = WeComMessage(
            msg_id=f"e2e_retry_{int(time.time()*1000)}",
            from_user=uid, msg_type="text", content="boom",
            create_time=int(time.time()),
        )
        event_id = _insert_inbound_event(msg)
        payload = {
            "msg_id": msg.msg_id, "from_userid": uid, "msg_type": "text",
            "content": "boom", "media_id": "",
            "create_time": msg.create_time, "inbound_event_id": event_id,
        }

        sent = []
        with patch("app.services.worker.WeComClient") as MockClient:
            client_inst = MagicMock()
            client_inst.send_text.side_effect = (
                lambda u, c: sent.append((u, c)) or {"errcode": 0}
            )
            MockClient.return_value = client_inst
            with patch("app.services.worker.message_router.process",
                       side_effect=RuntimeError("boom")):
                w = Worker()
                for i in range(3):
                    p = dict(payload); p["_retry_count"] = i
                    w._process_message(p)

        db = SessionLocal()
        try:
            row = db.query(WecomInboundEvent).filter_by(id=event_id).first()
            assert row.status == "dead_letter", f"got {row.status}"
            assert row.retry_count >= MAX_RETRY + 1
            assert row.error_message is not None
        finally:
            db.close()

        assert r.llen(QUEUE_DEAD_LETTER) == 1
        assert any("系统繁忙" in c for _, c in sent), f"no fallback reply, got {sent}"


def main():
    backend_dir = "/mnt/d/work/JobBridge/backend"
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    os.chdir(backend_dir)

    setup()

    test_redis_primitives()
    test_webhook_insert_and_enqueue()
    test_worker_help_command_roundtrip()
    test_worker_voice_file_image()
    test_p0_2_startup_recovery_preserves_media_id()
    test_p1_1_xu_prefix_strict()
    test_p1_2_rate_limit_notify_dedicated_queue()
    test_p1_3_invalidate_token()
    test_p1_4_broker_search_direction()
    test_my_status_command()
    test_idempotency_l2_db_unique()
    test_worker_retry_and_dead_letter()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"Total: {len(RESULTS)}  PASS={passed}  FAIL={failed}")
    if failed:
        print("\nFailures:")
        for name, ok, err in RESULTS:
            if not ok:
                print(f"  - {name}")
                print(f"    {err}")
        sys.exit(1)
    print("\nAll scenarios passed. Phase 4 pipeline verified.")


if __name__ == "__main__":
    main()
