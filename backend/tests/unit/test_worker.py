"""worker 单元测试（Phase 4）。

通过 mock 关键依赖（redis_client、message_router、WeComClient、SessionLocal）
验证：
- 消息处理 happy path
- 重试 / 死信流程
- 出站错误分类（token 过期刷新、用户不存在标记 inactive、限流进 send_retry）
- send_retry 队列消费（退避、最终放弃写 audit_log）
- 启动自检重入队列
- 心跳写入
"""
import json
import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from app.core.redis_client import SessionCommitDeadlineExceeded, UserLockUnavailable
from app.models import WecomOutboundOutbox
from app.schemas.conversation import ReplyMessage
from app.services.worker import (
    MAX_RETRY,
    MAX_SEND_RETRY,
    QUEUE_SEND_RETRY,
    SEND_RETRY_BACKOFFS,
    Worker,
    _build_outbox_claim_query,
    _build_wecom_message,
    _coerce_log_msg_type,
)
from app.wecom.client import WeComError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _basic_msg_data(msg_id="m1", userid="u1", content="你好", event_id=42):
    return {
        "msg_id": msg_id,
        "from_userid": userid,
        "msg_type": "text",
        "content": content,
        "media_id": "",
        "create_time": 1700000000,
        "inbound_event_id": event_id,
    }


@pytest.fixture
def worker():
    with patch("app.services.worker.get_redis"), \
         patch("app.services.worker.WeComClient"):
        instance = Worker()
        instance._last_recovery_scan = time.monotonic()
        instance._has_earlier_unfinished_event = MagicMock(return_value=False)
        return instance


# ---------------------------------------------------------------------------
# _build_wecom_message
# ---------------------------------------------------------------------------

class TestBuildMessage:
    def test_build_text(self):
        msg = _build_wecom_message(_basic_msg_data())
        assert msg.msg_id == "m1"
        assert msg.from_user == "u1"
        assert msg.msg_type == "text"
        assert msg.content == "你好"
        assert msg.image_url == ""


class TestCoerceLogMsgType:
    def test_text_passthrough(self):
        assert _coerce_log_msg_type("text") == "text"

    def test_image_passthrough(self):
        assert _coerce_log_msg_type("image") == "image"

    def test_file_maps_to_system(self):
        assert _coerce_log_msg_type("file") == "system"

    def test_event_maps_to_system(self):
        assert _coerce_log_msg_type("event") == "system"

    def test_unknown_maps_to_system(self):
        assert _coerce_log_msg_type("weird") == "system"


# ---------------------------------------------------------------------------
# 消息处理 happy path
# ---------------------------------------------------------------------------

class TestProcessMessageHappyPath:
    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    @patch("app.services.worker.user_lock")
    def test_happy_path_processes_and_marks_done(
        self, mock_lock_cm, mock_router, mock_session_factory, worker,
    ):
        # user_lock 返回 acquired=True
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=True)
        cm.__exit__ = MagicMock(return_value=False)
        mock_lock_cm.return_value = cm

        mock_router.process.return_value = [
            ReplyMessage(userid="u1", content="hello"),
        ]

        db = MagicMock()
        mock_session_factory.return_value = db

        with patch(
            "app.services.worker.conversation_service.begin_session_staging",
            return_value=MagicMock(),
        ), patch(
            "app.services.worker.conversation_service.end_session_staging",
            return_value=None,
        ), patch.object(
            worker, "_deliver_outbox_for_event", return_value=True,
        ) as deliver:
            worker._process_message(_basic_msg_data())

        mock_router.process.assert_called_once()
        deliver.assert_called_once_with(42)
        worker._wecom_client.send_text.assert_not_called()
        # processing marker + (router/log/outbox/done) atomic transaction
        assert db.commit.call_count == 2

    @patch("app.services.worker.log_event")
    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    @patch("app.services.worker.user_lock")
    def test_emits_queue_and_processing_latency(
        self, mock_lock_cm, mock_router, mock_session_factory, mock_log, worker,
    ):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=True)
        cm.__exit__ = MagicMock(return_value=False)
        mock_lock_cm.return_value = cm
        mock_router.process.return_value = []
        mock_session_factory.return_value = MagicMock()
        payload = _basic_msg_data()
        payload["_enqueued_at"] = time.time() - 2

        with patch(
            "app.services.worker.conversation_service.begin_session_staging",
            return_value=MagicMock(),
        ), patch(
            "app.services.worker.conversation_service.end_session_staging",
            return_value=None,
        ):
            worker._process_message(payload)

        fields = mock_log.call_args.kwargs
        assert mock_log.call_args.args[0] == "message_processing"
        assert fields["queue_wait_ms"] >= 1900
        assert fields["outcome"] == "processed"

    @patch("app.services.worker.log_event")
    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    @patch("app.services.worker.user_lock")
    def test_terminal_duplicate_is_skipped_before_router_and_send(
        self, mock_lock_cm, mock_router, mock_session_factory, mock_log, worker,
    ):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=True)
        cm.__exit__ = MagicMock(return_value=False)
        mock_lock_cm.return_value = cm
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = "done"
        mock_session_factory.return_value = db

        worker._process_message(_basic_msg_data())

        mock_router.process.assert_not_called()
        worker._wecom_client.send_text.assert_not_called()
        assert mock_log.call_args.kwargs["outcome"] == "duplicate_terminal_skipped"

    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    def test_lost_lock_allows_only_processing_marker_commit(
        self, mock_router, mock_session_factory, worker,
    ):
        mock_router.process.return_value = [
            ReplyMessage(userid="u1", content="hello"),
        ]
        db = MagicMock()
        mock_session_factory.return_value = db
        lease = MagicMock(spec=["assert_owned"])
        lease.assert_owned.side_effect = RuntimeError("lease lost")

        with patch.object(worker, "_handle_error") as handle_error:
            worker._process_locked(_basic_msg_data(), 42, 0, "u1", lease)

        # Only the independent worker_started_at/status marker is committed; router
        # business writes are rolled back after the lease assertion fails.
        assert db.commit.call_count == 1
        db.rollback.assert_called_once()
        worker._wecom_client.send_text.assert_not_called()
        handle_error.assert_called_once()


# ---------------------------------------------------------------------------
# 锁竞争重入
# ---------------------------------------------------------------------------

class TestDurableSessionCommit:
    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    def test_unattached_shadow_handle_is_discarded_after_busy_reply(
        self, mock_router, mock_session_factory, worker,
    ):
        db = MagicMock()
        mock_session_factory.return_value = db
        mock_router.process.return_value = [
            ReplyMessage(userid="u1", content="系统繁忙，请稍后再试。"),
        ]

        with patch(
            "app.services.worker.conversation_service.begin_session_staging",
            return_value=MagicMock(),
        ), patch(
            "app.services.worker.conversation_service.end_session_staging",
            return_value=None,
        ), patch(
            "app.services.worker.recommendation_shadow_service.begin_turn_tracking",
            return_value=MagicMock(),
        ), patch(
            "app.services.worker.recommendation_shadow_service.end_turn_tracking",
            return_value={"orphan-shadow-request"},
        ), patch(
            "app.services.worker.recommendation_shadow_service.discard",
        ) as discard, patch(
            "app.services.worker.recommendation_shadow_service.activate_persistence",
        ) as activate, patch.object(
            worker, "_deliver_outbox_for_event", return_value=True,
        ):
            outcome = worker._process_locked(
                _basic_msg_data(), 42, 0, "u1", MagicMock(),
            )

        assert outcome == "processed"
        discard.assert_called_once_with("orphan-shadow-request")
        activate.assert_not_called()

    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    def test_business_commit_failure_never_applies_staged_redis_session(
        self, mock_router, mock_session_factory, worker,
    ):
        db = MagicMock()
        db.commit.side_effect = [None, RuntimeError("mysql commit failed")]
        mock_session_factory.return_value = db
        mock_router.process.return_value = []
        commit = MagicMock()

        with patch(
            "app.services.worker.conversation_service.begin_session_staging",
            return_value=MagicMock(),
        ), patch(
            "app.services.worker.conversation_service.end_session_staging",
            return_value=commit,
        ), patch.object(
            worker, "_stage_session_commit",
        ), patch.object(
            worker, "_apply_session_commit_for_event",
        ) as apply_session, patch.object(
            worker, "_handle_error",
        ):
            outcome = worker._process_locked(
                _basic_msg_data(), 42, 0, "u1", MagicMock(),
            )

        assert outcome == "processing_failed"
        apply_session.assert_not_called()
        db.rollback.assert_called_once()

    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    def test_redis_apply_failure_leaves_outbox_hidden_and_returns_pending(
        self, mock_router, mock_session_factory, worker,
    ):
        db = MagicMock()
        mock_session_factory.return_value = db
        mock_router.process.return_value = [
            ReplyMessage(userid="u1", content="hello"),
        ]
        commit = MagicMock()

        with patch(
            "app.services.worker.conversation_service.begin_session_staging",
            return_value=MagicMock(),
        ), patch(
            "app.services.worker.conversation_service.end_session_staging",
            return_value=commit,
        ), patch.object(
            worker, "_stage_session_commit",
        ) as stage, patch.object(
            worker, "_apply_session_commit_for_event", return_value=False,
        ), patch.object(
            worker, "_deliver_outbox_for_event",
        ) as deliver:
            outcome = worker._process_locked(
                _basic_msg_data(), 42, 0, "u1", MagicMock(),
            )

        assert outcome == "session_commit_pending"
        stage.assert_called_once_with(db, 42, commit)
        deliver.assert_not_called()
        assert db.commit.call_count == 2

    def test_recovery_is_idempotent_after_redis_cas_before_db_mark(self, worker):
        item = {
            "event_id": 42,
            "attempts": 2,
            "lease_owner": "claim-1",
            "commit": MagicMock(),
        }
        with patch(
            "app.services.worker.conversation_service.apply_staged_session",
            return_value=False,
        ), patch(
            "app.services.worker.conversation_service.is_staged_session_applied",
            return_value=True,
        ), patch.object(
            worker, "_mark_session_commit_applied", return_value=True,
        ) as mark_applied, patch.object(
            worker, "_mark_session_commit_retry",
        ) as mark_retry:
            assert worker._apply_session_commit_item(item) is True

        mark_applied.assert_called_once_with(42, "claim-1")
        mark_retry.assert_not_called()

    @patch("app.services.worker.SessionLocal")
    def test_stale_session_claim_owner_cannot_mark_commit_applied(
        self, mock_session_factory, worker,
    ):
        db = MagicMock()
        query = db.query.return_value
        query.filter.return_value.update.return_value = 0
        mock_session_factory.return_value = db

        with patch("app.services.worker._promote_prepared_deliveries") as promote:
            assert worker._mark_session_commit_applied(42, "stale-owner") is False

        filters = " ".join(str(value) for value in query.filter.call_args.args)
        assert "session_apply_lease_owner" in filters
        assert "session_apply_locked_at" in filters
        promote.assert_not_called()

    @patch("app.services.worker.SessionLocal")
    def test_stale_session_claim_owner_cannot_schedule_retry(
        self, mock_session_factory, worker,
    ):
        db = MagicMock()
        query = db.query.return_value
        query.filter.return_value.update.return_value = 0
        mock_session_factory.return_value = db
        item = {
            "event_id": 42,
            "attempts": 2,
            "lease_owner": "stale-owner",
        }

        assert worker._mark_session_commit_retry(
            item, RuntimeError("retry"),
        ) is False

        filters = " ".join(str(value) for value in query.filter.call_args.args)
        assert "session_apply_lease_owner" in filters
        assert "session_apply_locked_at" in filters

    def test_expired_durable_session_commit_is_never_applied(self, worker):
        item = {
            "event_id": 42,
            "attempts": 1,
            "deadline_reached": True,
            "lease_owner": "claim-1",
            "commit": MagicMock(),
        }
        with patch(
            "app.services.worker.conversation_service.apply_staged_session",
        ) as apply_session, patch.object(
            worker, "_finish_expired_session_commit", return_value=False,
        ) as finish_expired:
            assert worker._apply_session_commit_item(item) is False

        apply_session.assert_not_called()
        finish_expired.assert_called_once()

    def test_redis_deadline_rejection_is_terminal_not_retryable(self, worker):
        item = {
            "event_id": 42,
            "attempts": 1,
            "deadline_reached": False,
            "lease_owner": "claim-1",
            "commit": MagicMock(),
        }
        with patch(
            "app.services.worker.conversation_service.apply_staged_session",
            side_effect=SessionCommitDeadlineExceeded("expired"),
        ), patch.object(
            worker, "_finish_expired_session_commit", return_value=False,
        ) as finish_expired, patch.object(
            worker, "_mark_session_commit_retry",
        ) as retry:
            assert worker._apply_session_commit_item(item) is False

        finish_expired.assert_called_once()
        retry.assert_not_called()

    def test_expired_commit_already_applied_finishes_database_checkpoint(self, worker):
        item = {
            "event_id": 42,
            "attempts": 2,
            "deadline_reached": True,
            "lease_owner": "claim-1",
            "payload_available": True,
            "commit": MagicMock(),
        }
        with patch(
            "app.services.worker.conversation_service.is_staged_session_applied",
            return_value=True,
        ), patch.object(
            worker, "_mark_session_commit_applied", return_value=True,
        ) as applied, patch.object(
            worker, "_mark_session_commit_terminal",
        ) as terminal:
            assert worker._finish_expired_session_commit(
                item, RuntimeError("deadline"),
            ) is True

        applied.assert_called_once_with(42, "claim-1")
        terminal.assert_not_called()

    def test_expired_commit_verification_failure_is_fail_closed(self, worker):
        item = {
            "event_id": 42,
            "attempts": 2,
            "deadline_reached": True,
            "lease_owner": "claim-1",
            "payload_available": True,
            "commit": MagicMock(),
        }
        with patch(
            "app.services.worker.conversation_service.is_staged_session_applied",
            side_effect=ConnectionError("redis unavailable"),
        ), patch.object(
            worker, "_mark_session_commit_terminal", return_value=True,
        ) as terminal, patch.object(
            worker, "_mark_session_commit_retry",
        ) as retry:
            assert worker._finish_expired_session_commit(
                item, RuntimeError("deadline"),
            ) is False

        assert terminal.call_args.kwargs["error_code"] == "session_commit_deadline"
        assert "could not be verified" in str(terminal.call_args.kwargs["error"])
        retry.assert_not_called()

    def test_expired_save_without_payload_does_not_read_redis(self, worker):
        item = {
            "event_id": 42,
            "attempts": 2,
            "deadline_reached": True,
            "lease_owner": "claim-1",
            "payload_available": False,
            "commit": MagicMock(operation="save", payload=None),
        }
        with patch(
            "app.services.worker.conversation_service.is_staged_session_applied",
        ) as is_applied, patch.object(
            worker, "_mark_session_commit_terminal", return_value=True,
        ) as terminal:
            assert worker._finish_expired_session_commit(
                item, RuntimeError("deadline"),
            ) is False

        is_applied.assert_not_called()
        assert terminal.call_args.kwargs["error_code"] == "session_commit_deadline"

    def test_expired_reconcile_terminalizes_when_redis_lock_is_unavailable(
        self, worker,
    ):
        item = {
            "event_id": 42,
            "userid": "u1",
            "attempts": 2,
            "deadline_reached": True,
            "lease_owner": "claim-1",
            "commit": MagicMock(),
        }
        lease = MagicMock()
        lease.__bool__.return_value = False
        lease.unavailable = True
        lock_context = MagicMock()
        lock_context.__enter__.return_value = lease
        lock_context.__exit__.return_value = False

        with patch.object(
            worker, "_claim_session_commits", return_value=[item],
        ), patch(
            "app.services.worker.user_lock", return_value=lock_context,
        ), patch.object(
            worker, "_mark_session_commit_terminal", return_value=True,
        ) as terminal, patch.object(
            worker, "_mark_session_commit_retry",
        ) as retry, patch.object(
            worker, "_apply_session_commit_item",
        ) as apply_commit:
            assert worker.reconcile_sessions_once(limit=1) == 1

        assert terminal.call_args.kwargs["error_code"] == "session_commit_deadline"
        assert "Redis user lock was unavailable" in str(
            terminal.call_args.kwargs["error"],
        )
        retry.assert_not_called()
        apply_commit.assert_not_called()

    def test_expired_reconcile_retries_when_user_lock_is_busy(self, worker):
        item = {
            "event_id": 42,
            "userid": "u1",
            "attempts": 2,
            "deadline_reached": True,
            "lease_owner": "claim-1",
            "commit": MagicMock(),
        }
        lease = MagicMock()
        lease.__bool__.return_value = False
        lease.unavailable = False
        lock_context = MagicMock()
        lock_context.__enter__.return_value = lease
        lock_context.__exit__.return_value = False

        with patch.object(
            worker, "_claim_session_commits", return_value=[item],
        ), patch(
            "app.services.worker.user_lock", return_value=lock_context,
        ), patch.object(
            worker, "_mark_session_commit_terminal",
        ) as terminal, patch.object(
            worker, "_mark_session_commit_retry",
        ) as retry, patch.object(
            worker, "_apply_session_commit_item",
        ) as apply_commit:
            assert worker.reconcile_sessions_once(limit=1) == 1

        retry.assert_called_once()
        assert str(retry.call_args.args[1]) == "user lock busy"
        terminal.assert_not_called()
        apply_commit.assert_not_called()

    def test_expired_reconcile_terminalizes_when_lock_verification_is_unavailable(
        self, worker,
    ):
        item = {
            "event_id": 42,
            "userid": "u1",
            "attempts": 2,
            "deadline_reached": True,
            "lease_owner": "claim-1",
            "commit": MagicMock(),
        }
        lease = MagicMock()
        lease.__bool__.return_value = True
        lease.assert_owned = MagicMock(
            side_effect=UserLockUnavailable("redis down"),
        )
        lock_context = MagicMock()
        lock_context.__enter__.return_value = lease
        lock_context.__exit__.return_value = False

        with patch.object(
            worker, "_claim_session_commits", return_value=[item],
        ), patch(
            "app.services.worker.user_lock", return_value=lock_context,
        ), patch.object(
            worker, "_mark_session_commit_terminal", return_value=True,
        ) as terminal, patch.object(
            worker, "_mark_session_commit_retry",
        ) as retry, patch.object(
            worker, "_apply_session_commit_item",
        ) as apply_commit:
            assert worker.reconcile_sessions_once(limit=1) == 1

        assert terminal.call_args.kwargs["error_code"] == "session_commit_deadline"
        assert isinstance(terminal.call_args.kwargs["error"], UserLockUnavailable)
        retry.assert_not_called()
        apply_commit.assert_not_called()

    def test_terminal_session_commit_closes_outbox_and_delivery_gates(self, worker):
        db = MagicMock()
        row = MagicMock(id=42)
        prepared = MagicMock(status="prepared")
        sent = MagicMock(status="sent")
        pending_outbox = MagicMock(
            status="pending", recommendation_delivery_id="delivery-1",
        )
        sending_outbox = MagicMock(
            status="sending", recommendation_delivery_id="delivery-1",
        )

        with patch.object(
            worker,
            "_lock_outboxes_for_event",
            return_value=[pending_outbox, sending_outbox],
        ) as lock_outboxes, patch.object(
            worker, "_lock_deliveries_by_id", return_value=[prepared, sent],
        ) as lock_deliveries, patch(
            "app.services.recommendation_delivery_service.purge_delivery_content",
        ) as purge:
            worker._terminalize_session_commit_locked(
                db,
                row,
                error_code="session_commit_deadline",
                error=RuntimeError("deadline"),
            )

        assert prepared.status == "permanent_failed"
        assert prepared.last_error_code == "session_commit_deadline"
        lock_outboxes.assert_called_once_with(db, 42)
        lock_deliveries.assert_called_once_with(db, ["delivery-1"])
        assert purge.call_args_list == [call(prepared), call(sent)]
        db.flush.assert_called_once()
        assert pending_outbox.status == "dead_letter"
        assert pending_outbox.locked_at is None
        assert pending_outbox.next_attempt_at is None
        assert "session_commit_deadline" in pending_outbox.last_error
        assert sending_outbox.status == "dead_letter"
        assert "ambiguous provider outcome" in sending_outbox.last_error
        assert row.status == "dead_letter"
        assert row.session_payload is None

    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    def test_post_commit_exception_never_requeues_business_router(
        self, mock_router, mock_session_factory, worker,
    ):
        db = MagicMock()
        mock_session_factory.return_value = db
        mock_router.process.return_value = []

        with patch(
            "app.services.worker.conversation_service.begin_session_staging",
            return_value=MagicMock(),
        ), patch(
            "app.services.worker.conversation_service.end_session_staging",
            return_value=None,
        ), patch.object(
            worker,
            "_deliver_outbox_for_event",
            side_effect=RuntimeError("recovery database unavailable"),
        ), patch.object(worker, "_handle_error") as handle_error:
            outcome = worker._process_locked(
                _basic_msg_data(), 42, 0, "u1", MagicMock(),
            )

        assert outcome == "post_commit_recovery_pending"
        handle_error.assert_not_called()
        mock_router.process.assert_called_once()


class TestLockContention:
    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.user_lock")
    def test_lock_busy_requeues(self, mock_lock_cm, mock_enq, worker):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=False)
        cm.__exit__ = MagicMock(return_value=False)
        mock_lock_cm.return_value = cm

        with patch("app.services.worker.time.sleep"):
            worker._process_message(_basic_msg_data())

        mock_enq.assert_called_once()
        args, _ = mock_enq.call_args
        payload = json.loads(args[0])
        assert payload["from_userid"] == "u1"

    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.user_lock")
    def test_later_same_user_event_requeues_without_processing(
        self, mock_lock_cm, mock_enq, worker,
    ):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=True)
        cm.__exit__ = MagicMock(return_value=False)
        mock_lock_cm.return_value = cm
        worker._has_earlier_unfinished_event.return_value = True

        with patch.object(worker, "_process_locked") as process_locked:
            worker._process_message(_basic_msg_data(event_id=43))

        process_locked.assert_not_called()
        mock_enq.assert_called_once()

    @patch("app.services.worker.enqueue_message", side_effect=RuntimeError("down"))
    @patch("app.services.worker.user_lock")
    def test_ordered_requeue_failure_preserves_durable_event(
        self, mock_lock_cm, _mock_enq, worker,
    ):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=True)
        cm.__exit__ = MagicMock(return_value=False)
        mock_lock_cm.return_value = cm
        worker._has_earlier_unfinished_event.return_value = True

        with patch.object(worker, "_preserve_event_for_recovery") as preserve:
            worker._process_message(_basic_msg_data(event_id=43))

        preserve.assert_called_once_with(43)


class TestAuxQueueFairness:
    def test_busy_inbound_loop_still_services_aux_queues(self, worker, monkeypatch):
        monkeypatch.setattr("app.services.worker.AUX_QUEUE_EVERY_MESSAGES", 1)
        worker._redis.blpop.return_value = (
            "queue:incoming",
            json.dumps(_basic_msg_data()),
        )

        def stop_after_one(_payload):
            worker._running = False

        with patch.object(worker, "_process_message", side_effect=stop_after_one), \
             patch.object(worker, "_service_aux_queues") as service:
            worker._main_loop()

        service.assert_called_once()


# ---------------------------------------------------------------------------
# 错误处理：重试 / 死信
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @patch("app.services.worker.enqueue_message")
    def test_retry_under_max_requeues_with_incremented_count(
        self, mock_enq, worker,
    ):
        with patch.object(worker, "_mark_event_fail") as mock_fail, \
             patch.object(worker, "_update_retry_and_error_keep_processing") as mock_keep:
            worker._handle_error(
                _basic_msg_data(), event_id=42, retry_count=0,
                error=RuntimeError("boom"),
            )
        mock_enq.assert_called_once()
        args, _ = mock_enq.call_args
        payload = json.loads(args[0])
        assert payload["_retry_count"] == 1
        mock_fail.assert_called_once()
        mock_keep.assert_not_called()

    @patch("app.services.worker.enqueue_message")
    def test_retry_at_max_goes_to_dead_letter(self, mock_enq, worker):
        with patch.object(worker, "_mark_event_fail"):
            worker._handle_error(
                _basic_msg_data(), event_id=42, retry_count=MAX_RETRY,
                error=RuntimeError("boom"),
            )
        # 推入死信队列
        args, _ = mock_enq.call_args
        from app.services.worker import QUEUE_DEAD_LETTER
        assert args[1] == QUEUE_DEAD_LETTER
        # 尝试发送兜底回复
        worker._wecom_client.send_text.assert_called()

    @patch("app.services.worker.enqueue_message", side_effect=Exception("redis down"))
    def test_p0_1_retry_enqueue_failure_keeps_processing_status(
        self, mock_enq, worker,
    ):
        """P0-1：retry 阶段入队失败时不得把 status 改成 failed，
        必须保持 processing 让 startup_recovery 兜底重入队，避免消息丢失。"""
        with patch.object(worker, "_mark_event_fail") as mock_fail, \
             patch.object(
                 worker, "_update_retry_and_error_keep_processing"
             ) as mock_keep:
            worker._handle_error(
                _basic_msg_data(), event_id=42, retry_count=0,
                error=RuntimeError("boom"),
            )
        # 入队失败：不应标 failed（那会让 startup_recovery 扫不到）
        mock_fail.assert_not_called()
        # 应该只更新 retry_count/error，保持 processing
        mock_keep.assert_called_once()
        args, kwargs = mock_keep.call_args
        assert args[0] == 42
        assert args[1] == 1  # retry_count + 1

    @patch("app.services.worker.SessionLocal")
    def test_failed_retry_handoff_becomes_due_on_next_recovery_scan(
        self, mock_factory, worker,
    ):
        db = MagicMock()
        update = db.query.return_value.filter.return_value.update
        mock_factory.return_value = db

        worker._update_retry_and_error_keep_processing(
            42, 1, "RedisError: unavailable",
        )

        values = update.call_args.args[0]
        assert values["status"] == "processing"
        assert "worker_started_at" in values
        assert values["retry_count"] == 1
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# 出站错误分类
# ---------------------------------------------------------------------------

class TestSendErrorHandling:
    def test_token_expired_refreshes_and_retries(self, worker):
        """P1-3：token 过期时应走公开 invalidate_token()，不再触碰私有字段。"""
        call_count = {"n": 0}

        def _send(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise WeComError("token expired", errcode=42001)
            return {"errcode": 0}

        worker._wecom_client.send_text.side_effect = _send
        reply = ReplyMessage(userid="u1", content="hi")
        ok = worker._send_one(reply)
        assert ok is True
        assert call_count["n"] == 2
        # 通过公开方法失效缓存，而非直接改私有属性
        worker._wecom_client.invalidate_token.assert_called_once()

    @patch("app.services.worker.SessionLocal")
    def test_user_unreachable_marks_inactive_no_retry(self, mock_factory, worker):
        db = MagicMock()
        user = MagicMock()
        user.extra = None  # 初始 extra 为 NULL
        db.query.return_value.filter.return_value.first.return_value = user
        mock_factory.return_value = db
        worker._wecom_client.send_text.side_effect = WeComError(
            "user not found", errcode=60111,
        )
        reply = ReplyMessage(userid="u1", content="hi")
        ok = worker._send_one(reply)
        assert ok is False
        # 不再修改 user.status（保持 active），只打 extra 标
        assert user.extra is not None
        assert user.extra.get("wecom_unreachable") is True
        assert "wecom_unreachable_at" in user.extra
        db.commit.assert_called_once()

    def test_rate_limit_enqueues_send_retry(self, worker):
        worker._wecom_client.send_text.side_effect = WeComError(
            "rate limited", errcode=45009,
        )
        worker._redis.rpush = MagicMock()
        reply = ReplyMessage(userid="u1", content="hi")
        ok = worker._send_one(reply)
        assert ok is False
        worker._redis.rpush.assert_called_once()
        args, _ = worker._redis.rpush.call_args
        assert args[0] == QUEUE_SEND_RETRY

    def test_generic_exception_enqueues_send_retry(self, worker):
        worker._wecom_client.send_text.side_effect = RuntimeError("network down")
        worker._redis.rpush = MagicMock()
        reply = ReplyMessage(userid="u1", content="hi")
        ok = worker._send_one(reply)
        assert ok is False
        worker._redis.rpush.assert_called_once()


# ---------------------------------------------------------------------------
# durable transactional outbox
# ---------------------------------------------------------------------------

class TestTransactionalOutbox:
    def test_claim_sql_locks_only_outer_outbox_rows(self):
        db = Session()
        query = _build_outbox_claim_query(
            db,
            WecomOutboundOutbox.status == "pending",
            limit=5,
        )

        sql = " ".join(str(query.statement.compile(
            dialect=mysql.dialect(),
        )).lower().split())
        outer_from = sql.split(" where ", 1)[0]

        assert outer_from.endswith("from wecom_outbound_outbox")
        assert " join " not in outer_from
        assert sql.count("exists (select") >= 3
        assert "order by wecom_outbound_outbox.id" in sql
        assert sql.endswith("for update skip locked")

    def test_stage_outbox_preserves_reply_order_and_metadata(self, worker):
        from app.models import WecomOutboundOutbox

        db = MagicMock()
        replies = [
            ReplyMessage(
                userid="u1",
                content="first",
                intent="search_job",
                criteria_snapshot={"criteria": {"city": ["苏州市"]}},
            ),
            ReplyMessage(userid="u1", content="second"),
        ]

        worker._stage_outbox(db, 42, replies)

        rows = [call.args[0] for call in db.add.call_args_list]
        assert all(isinstance(row, WecomOutboundOutbox) for row in rows)
        assert [row.reply_index for row in rows] == [0, 1]
        assert rows[0].inbound_event_id == 42
        assert rows[0].intent == "search_job"
        assert rows[0].criteria_snapshot["criteria"]["city"] == ["苏州市"]

    def test_event_delivery_attempts_every_claim_even_if_one_fails(self, worker):
        claimed = [
            {"id": 1, "userid": "u1", "content": "a", "attempt_count": 1},
            {"id": 2, "userid": "u1", "content": "b", "attempt_count": 1},
        ]
        with patch.object(worker, "_claim_outbox", side_effect=[claimed, []]), \
             patch.object(
                 worker, "_deliver_outbox_item", side_effect=[False, True],
             ) as deliver:
            ok = worker._deliver_outbox_for_event(42)

        assert ok is False
        assert deliver.call_count == 2

    def test_success_records_provider_message_id(self, worker):
        item = {"id": 1, "userid": "u1", "content": "a", "attempt_count": 1}
        worker._wecom_client.send_text.return_value = {"errcode": 0, "msgid": "wx-1"}

        with patch.object(worker, "_mark_outbox_sent", return_value=True) as sent:
            assert worker._deliver_outbox_item(item) is True

        sent.assert_called_once_with(1, "wx-1")

    def test_network_failure_returns_row_to_durable_retry(self, worker):
        item = {"id": 1, "userid": "u1", "content": "a", "attempt_count": 1}
        error = RuntimeError("timeout after send")
        worker._wecom_client.send_text.side_effect = error

        with patch.object(worker, "_mark_outbox_failed") as failed:
            assert worker._deliver_outbox_item(item) is False

        failed.assert_called_once_with(item, error)

    @patch("app.services.worker.SessionLocal")
    @patch("app.services.worker.message_router")
    def test_atomic_commit_failure_never_attempts_outbox_delivery(
        self, mock_router, mock_session_factory, worker,
    ):
        mock_router.process.return_value = [
            ReplyMessage(userid="u1", content="hello"),
        ]
        db = MagicMock()
        db.commit.side_effect = [None, RuntimeError("atomic commit failed")]
        mock_session_factory.return_value = db
        lease = MagicMock(spec=["assert_owned"])

        with patch.object(worker, "_handle_error") as handle_error, \
             patch.object(worker, "_deliver_outbox_for_event") as deliver:
            worker._process_locked(_basic_msg_data(), 42, 0, "u1", lease)

        deliver.assert_not_called()
        db.rollback.assert_called_once()
        handle_error.assert_called_once()


# ---------------------------------------------------------------------------
# send_retry 队列消费
# ---------------------------------------------------------------------------

class TestRateLimitNotifyQueue:
    """P1-2：限流通知走专用队列，发失败即丢，不与 send_retry 混用。"""

    def test_best_effort_send_success(self, worker):
        payload = {"userid": "u1", "content": "您发送太频繁了"}
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()
        worker._wecom_client.send_text.return_value = {"errcode": 0}

        worker._process_rate_limit_notify_once()

        worker._wecom_client.send_text.assert_called_once_with("u1", "您发送太频繁了")
        # 不应重入任何队列
        worker._redis.rpush.assert_not_called()

    def test_send_failure_drops_without_retry(self, worker):
        payload = {"userid": "u1", "content": "您发送太频繁了"}
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()
        worker._wecom_client.send_text.side_effect = RuntimeError("network")

        worker._process_rate_limit_notify_once()

        # 静默丢弃，绝不重试入 send_retry
        worker._redis.rpush.assert_not_called()


class TestSendRetryQueue:
    def test_backoff_not_reached_requeues_without_send(self, worker):
        payload = {
            "userid": "u1", "content": "x",
            "send_retry_count": 0,
            "backoff_until": time.time() + 60,  # 还未到
        }
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()

        with patch("app.services.worker.time.sleep"):
            worker._process_send_retry_once()

        worker._redis.rpush.assert_called_once()
        worker._wecom_client.send_text.assert_not_called()

    def test_success_drops_from_queue(self, worker):
        payload = {
            "userid": "u1", "content": "x",
            "send_retry_count": 0,
            "backoff_until": 0,
        }
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()
        worker._wecom_client.send_text.return_value = {"errcode": 0}

        worker._process_send_retry_once()

        worker._wecom_client.send_text.assert_called_once_with("u1", "x")
        worker._redis.rpush.assert_not_called()

    def test_retry_at_max_writes_audit_log(self, worker):
        payload = {
            "userid": "u1", "content": "x",
            "send_retry_count": MAX_SEND_RETRY - 1,
            "backoff_until": 0,
        }
        worker._redis.lpop = MagicMock(return_value=json.dumps(payload))
        worker._redis.rpush = MagicMock()
        worker._wecom_client.send_text.side_effect = RuntimeError("perm fail")

        with patch.object(worker, "_write_send_failed_audit") as mock_audit:
            worker._process_send_retry_once()

        mock_audit.assert_called_once()
        # 达到上限后不再重入队列
        worker._redis.rpush.assert_not_called()


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------

class TestStartupRecovery:
    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_requeues_processing_rows(self, mock_factory, mock_enq, worker):
        db = MagicMock()
        zombie = MagicMock()
        zombie.msg_id = "m1"
        zombie.from_userid = "u1"
        zombie.msg_type = "text"
        zombie.content_brief = "hello"
        zombie.media_id = None
        zombie.id = 42
        zombie.retry_count = 0
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        db.query.return_value.filter.return_value.with_for_update.return_value.limit.return_value.all.return_value = [zombie]
        mock_factory.return_value = db

        worker._startup_recovery()

        assert mock_enq.call_count == 1
        assert zombie.status == "processing"
        assert zombie.worker_started_at is not None
        db.commit.assert_called_once()

    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_successful_scan_advances_interval_and_repeat_scan_is_empty(
        self, mock_factory, mock_enq, worker,
    ):
        db = MagicMock()
        zombie = MagicMock()
        zombie.msg_id = "m-repeat"
        zombie.from_userid = "u1"
        zombie.msg_type = "text"
        zombie.content_brief = "hello"
        zombie.media_id = None
        zombie.id = 43
        zombie.retry_count = 0
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        query = (
            db.query.return_value.filter.return_value
            .with_for_update.return_value.limit.return_value
        )
        query.all.side_effect = [[zombie], []]
        mock_factory.return_value = db
        before = time.monotonic()

        worker._startup_recovery()
        first_scan_at = worker._last_recovery_scan
        worker._startup_recovery()

        assert mock_enq.call_count == 1
        assert first_scan_at >= before
        assert worker._last_recovery_scan >= first_scan_at

    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_scan_failure_is_interval_bounded(
        self, mock_factory, mock_enq, worker,
    ):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db unavailable")
        mock_factory.return_value = db
        before = time.monotonic()

        worker._startup_recovery()

        mock_enq.assert_not_called()
        assert worker._last_recovery_scan >= before
        db.rollback.assert_called_once()

    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_claim_commit_failure_never_enqueues(
        self, mock_factory, mock_enq, worker,
    ):
        db = MagicMock()
        zombie = MagicMock(
            msg_id="m-commit-fail",
            from_userid="u1",
            msg_type="text",
            content_brief="hello",
            media_id=None,
            id=44,
            retry_count=0,
        )
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        (
            db.query.return_value.filter.return_value
            .with_for_update.return_value.limit.return_value.all
        ).return_value = [zombie]
        db.commit.side_effect = RuntimeError("commit failed")
        mock_factory.return_value = db

        worker._startup_recovery()

        mock_enq.assert_not_called()
        db.rollback.assert_called_once()

    @patch(
        "app.services.worker.enqueue_message",
        side_effect=RuntimeError("redis unavailable"),
    )
    @patch("app.services.worker.SessionLocal")
    def test_enqueue_failure_leaves_committed_processing_claim(
        self, mock_factory, mock_enq, worker,
    ):
        db = MagicMock()
        zombie = MagicMock(
            msg_id="m-redis-fail",
            from_userid="u1",
            msg_type="text",
            content_brief="hello",
            media_id=None,
            id=45,
            retry_count=0,
        )
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        (
            db.query.return_value.filter.return_value
            .with_for_update.return_value.limit.return_value.all
        ).return_value = [zombie]
        mock_factory.return_value = db

        with patch.object(worker, "_mark_event_recovery_due") as mark_due:
            worker._startup_recovery()

        mock_enq.assert_called_once()
        db.commit.assert_called_once()
        db.rollback.assert_not_called()
        assert zombie.status == "processing"
        mark_due.assert_called_once_with(
            45, error_message="startup recovery enqueue failed",
        )

    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_requeues_image_preserves_media_id(self, mock_factory, mock_enq, worker):
        """P0-2：图片消息恢复时 media_id 与原始 msg_type 必须回写到队列 payload。"""
        db = MagicMock()
        zombie = MagicMock()
        zombie.msg_id = "m-img"
        zombie.from_userid = "u1"
        zombie.msg_type = "image"
        zombie.content_brief = "[image] media_id saved"
        zombie.media_id = "MEDIA_XYZ"
        zombie.id = 99
        zombie.retry_count = 0
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        db.query.return_value.filter.return_value.with_for_update.return_value.limit.return_value.all.return_value = [zombie]
        mock_factory.return_value = db

        worker._startup_recovery()

        assert mock_enq.call_count == 1
        payload = json.loads(mock_enq.call_args[0][0])
        assert payload["msg_type"] == "image"
        assert payload["media_id"] == "MEDIA_XYZ"
        # 媒体消息不把 content_brief 透传为 text content
        assert payload["content"] == ""

    @patch("app.services.worker.enqueue_message")
    @patch("app.services.worker.SessionLocal")
    def test_requeues_file_preserves_raw_msg_type(self, mock_factory, mock_enq, worker):
        """P1-5：file/video/link/location 恢复时保持原始 msg_type，不再被 coerced 成 event。"""
        db = MagicMock()
        zombie = MagicMock()
        zombie.msg_id = "m-file"
        zombie.from_userid = "u1"
        zombie.msg_type = "file"
        zombie.content_brief = "[file] media_id saved"
        zombie.media_id = "FILE_XYZ"
        zombie.id = 100
        zombie.retry_count = 0
        from datetime import datetime, timezone
        zombie.created_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
        db.query.return_value.filter.return_value.with_for_update.return_value.limit.return_value.all.return_value = [zombie]
        mock_factory.return_value = db

        worker._startup_recovery()

        payload = json.loads(mock_enq.call_args[0][0])
        assert payload["msg_type"] == "file"
        assert payload["media_id"] == "FILE_XYZ"


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------

class TestConversationLog:
    def test_inbound_and_outbound_written_with_snapshot(self, worker):
        """出站 ReplyMessage 的 intent/criteria_snapshot 应被写入 conversation_log。"""
        from app.services.worker import ConversationLog
        db = MagicMock()

        msg = _build_wecom_message(_basic_msg_data(msg_id="m1", userid="u1"))
        replies = [
            ReplyMessage(
                userid="u1", content="3 条结果",
                intent="search_job",
                criteria_snapshot={"criteria": {"city": ["苏州市"]}, "prompt_version": "v2.0"},
            ),
        ]
        worker._write_conversation_log(db, msg, replies)

        # nested transaction 用作 UNIQUE 冲突保护
        assert db.begin_nested.call_count == 2
        # 两次 add：入站 + 出站
        assert db.add.call_count == 2


class TestHeartbeat:
    def test_heartbeat_writes_key_with_ttl(self, worker):
        worker._redis.set = MagicMock()
        # 启动心跳线程并立即设置退出，避免实际循环 60s
        started = threading.Event()

        original_set = worker._redis.set

        def _set_wrapped(*args, **kwargs):
            original_set(*args, **kwargs)
            started.set()
            worker._running = False

        worker._redis.set = _set_wrapped
        worker._start_heartbeat()
        assert started.wait(timeout=3.0)
        # Thread 会检查 self._running 后退出
        worker._heartbeat_thread.join(timeout=5.0)
        assert not worker._heartbeat_thread.is_alive()
