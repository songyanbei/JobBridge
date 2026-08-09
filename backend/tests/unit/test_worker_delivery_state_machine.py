"""推荐投递状态机 / 企微返回体 / 独立恢复线程 / TTL 的回归测试。

覆盖 code review 条目：

- P1-11 delivery 状态枚举收敛到 §9.6 行 1921 的
  ``prepared/pending/sending/retry_wait/sent/permanent_failed/unknown``；
- P1-12 消费 ``send_text`` 返回体，单用户命中 invaliduser/unlicenseduser 不标 sent；
- P1-15 dispatcher/reconciler/deriver 是独立线程，且 sent 后派生必须走 claim；
- P1-17 推荐明细 90 天 TTL 按 §9.11 行 2133-2143 的固定顺序删除；
- P2-8 解密失败保持可恢复状态并告警；P2-12 TTL 的 CASE 纳入 retry_wait。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import worker as worker_module
from app.services.worker import Worker
from app.tasks import ttl_cleanup
from app.wecom.client import (
    WeComError,
    parse_invalid_recipients,
    recipient_rejected,
    whitelist_send_response,
)

_OK_RESPONSE = {
    "errcode": 0,
    "errmsg": "ok",
    "msgid": "wx-1",
    "response_code": "",
    "invaliduser": "",
    "unlicenseduser": "",
}


@pytest.fixture
def worker():
    with patch("app.services.worker.get_redis"), \
         patch("app.services.worker.WeComClient"):
        instance = Worker()
        instance._last_recovery_scan = time.monotonic()
        return instance


def _delivery_item(**overrides) -> dict:
    item = {
        "id": 1,
        "userid": "u1",
        "content": "推荐正文",
        "recommendation_delivery_id": "d-1",
        "attempt_count": 1,
    }
    item.update(overrides)
    return item


def _valid_context(
    *, target_type: str = "job", target_id: int = 7,
) -> dict:
    return {
        "assignment": "stable",
        "algorithm_version": "recommendation-v1",
        "query_digest": "digest",
        "items": [{
            "target_type": target_type,
            "target_id": target_id,
            "position": 1,
        }],
    }


def _active_target(*, target_type: str = "job") -> SimpleNamespace:
    values = {
        "audit_status": "passed",
        "deleted_at": None,
        "expires_at": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
    }
    if target_type == "job":
        values["delist_reason"] = None
    return SimpleNamespace(**values)


def _claim_db(
    delivery: MagicMock,
    *,
    target: object | None = None,
    claim_updated: int = 1,
) -> MagicMock:
    db = MagicMock()
    db.get.return_value = delivery
    target_query = MagicMock()
    target_query.populate_existing.return_value.filter.return_value.with_for_update.return_value.first.return_value = target
    delivery_query = MagicMock()
    delivery_query.populate_existing.return_value.filter.return_value.with_for_update.return_value.first.return_value = MagicMock()
    delivery_query.filter.return_value.update.return_value = claim_updated
    db.query.side_effect = lambda model: (
        target_query
        if model in (worker_module.Job, worker_module.Resume)
        else delivery_query
    )
    return db


def _update_payloads(db: MagicMock) -> list[dict]:
    """收集这次调用里所有 ``query(...).filter(...).update(values)`` 的 values。"""
    calls = db.query.return_value.filter.return_value.update.call_args_list
    return [call.args[0] for call in calls if call.args]


# ---------------------------------------------------------------------------
# P1-12：企微返回体
# ---------------------------------------------------------------------------

class TestWecomSendResponse:
    def test_whitelist_drops_everything_outside_the_contract(self):
        payload = whitelist_send_response({
            "errcode": 0,
            "errmsg": "ok",
            "response_code": "rc",
            "msgid": "wx-1",
            "access_token": "SECRET",
            "invaliduser": "u1",
        })
        assert payload == {"errcode": 0, "errmsg": "ok", "response_code": "rc"}

    def test_parse_splits_pipe_separated_recipients(self):
        assert parse_invalid_recipients({
            "invaliduser": "u1|u2", "unlicenseduser": "",
        }) == {"invaliduser": ["u1", "u2"]}

    def test_recipient_rejected_detects_partial_failure(self):
        assert recipient_rejected({"invaliduser": "u1"}, "u1") == "invaliduser"
        assert recipient_rejected({"unlicenseduser": "u1"}, "u1") == "unlicenseduser"
        assert recipient_rejected(_OK_RESPONSE, "u1") is None

    def test_single_recipient_in_invalid_list_is_not_marked_sent(self, worker):
        """errcode=0 但用户在 invaliduser 里：消息没送达，绝不能派生曝光。"""
        item = _delivery_item()
        worker._wecom_client.send_text.return_value = {
            "errcode": 0, "errmsg": "ok", "msgid": "wx-1", "invaliduser": "u1",
        }

        with patch.object(worker, "_mark_delivery_sent") as sent, \
             patch.object(worker, "_mark_outbox_sent") as outbox_sent, \
             patch.object(worker, "_mark_outbox_failed") as failed, \
             patch.object(worker, "_mark_user_inactive") as inactive:
            assert worker._deliver_outbox_item(item) is False

        sent.assert_not_called()
        outbox_sent.assert_not_called()
        inactive.assert_called_once_with("u1")
        assert failed.call_args.kwargs["terminal"] is True
        assert failed.call_args.kwargs["response"]["invaliduser"] == "u1"
        assert failed.call_args.kwargs["error_code"] == "invaliduser"

    def test_clean_response_persists_msgid_and_whitelist(self, worker):
        item = _delivery_item()
        worker._wecom_client.send_text.return_value = dict(_OK_RESPONSE)
        db = MagicMock()
        db.query.return_value.filter.return_value.update.return_value = 1

        with patch("app.services.worker.SessionLocal", return_value=db), \
             patch.object(worker, "_submit_immediate_impressions") as submit:
            assert worker._deliver_outbox_item(item) is True

        payloads = _update_payloads(db)
        delivery_values = [v for v in payloads if v.get("status") == "sent"][-1]
        assert delivery_values["wecom_msgid"] == "wx-1"
        assert delivery_values["wecom_response"] == {
            "errcode": 0, "errmsg": "ok", "response_code": "",
        }
        # 空的部分失败字段不应该写成 {} 噪音
        assert delivery_values["invalid_recipients"] is None
        submit.assert_called_once()


# ---------------------------------------------------------------------------
# P1-11：状态机枚举
# ---------------------------------------------------------------------------

class TestDeliveryStatusMachine:
    def test_enum_matches_plan_section_9_6(self):
        assert worker_module.DELIVERY_ACTIVE_STATUSES == (
            "prepared", "pending", "sending", "retry_wait",
        )
        assert worker_module.DELIVERY_SENDABLE_STATUSES == ("pending", "retry_wait")

    def test_retryable_failure_writes_retry_wait_with_next_attempt(self, worker):
        db = MagicMock()
        with patch("app.services.worker.SessionLocal", return_value=db):
            worker._mark_outbox_failed(
                _delivery_item(), RuntimeError("timeout"),
            )

        payloads = _update_payloads(db)
        # 先 outbox 回 pending，再把 delivery 写成 retry_wait（旧实现写的是 pending）
        assert [v.get("status") for v in payloads] == ["pending", "retry_wait"]
        assert "next_attempt_at" in payloads[1]
        assert payloads[1]["last_error_code"] == "RuntimeError"

    def test_terminal_failure_writes_permanent_failed_not_dead_letter(self, worker):
        db = MagicMock()
        with patch("app.services.worker.SessionLocal", return_value=db), \
             patch.object(worker, "_write_send_failed_audit"):
            worker._mark_outbox_failed(
                _delivery_item(),
                WeComError("user not found", errcode=60111),
                terminal=True,
            )

        statuses = {v.get("status") for v in _update_payloads(db)}
        assert "permanent_failed" in statuses
        # outbox 自己的枚举保留 dead_letter；delivery 不允许出现它。
        assert "dead_letter" in statuses
        delivery_values = [
            v for v in _update_payloads(db) if v.get("status") == "permanent_failed"
        ][0]
        assert delivery_values["last_error_code"] == "60111"

    def test_missing_body_marks_permanent_failed(self, worker):
        delivery = MagicMock(
            status="pending",
            content_ciphertext=None,
            recommendation_context=_valid_context(),
            created_at=datetime.now(timezone.utc),
        )
        db = _claim_db(delivery, target=_active_target())
        row = MagicMock(recommendation_delivery_id="d-1", attempt_count=0)

        assert worker._claim_recommendation_body(db, row, MagicMock()) is None
        assert delivery.status == "permanent_failed"
        assert row.status == "dead_letter"

    def test_prepared_delivery_is_deferred_not_terminated(self, worker):
        delivery = MagicMock(
            status="prepared",
            content_ciphertext=b"x",
            recommendation_context=_valid_context(),
        )
        db = _claim_db(delivery, target=_active_target())
        row = MagicMock(recommendation_delivery_id="d-1", attempt_count=0)

        assert worker._claim_recommendation_body(db, row, MagicMock()) is None
        assert row.status == "pending"

    def test_decrypt_failure_keeps_retry_wait_and_alerts(self, worker):
        """P2-8：解密失败 fail-closed，但不得终态化。"""
        delivery = MagicMock(
            status="pending",
            content_ciphertext=b"bad",
            attempt_count=0,
            recommendation_context=_valid_context(),
        )
        db = _claim_db(delivery, target=_active_target())
        row = MagicMock(recommendation_delivery_id="d-1", attempt_count=0)

        with patch(
            "app.services.recommendation_delivery_service.decrypt_delivery_body",
            side_effect=ValueError("bad tag"),
        ), patch("app.services.worker.log_event") as log:
            assert worker._claim_recommendation_body(db, row, MagicMock()) is None

        assert delivery.status == "retry_wait"
        assert delivery.last_error_code == "content_decrypt_failed"
        assert row.status == "pending"  # outbox 行仍可恢复
        assert log.call_args.args[0] == "recommendation_delivery_decrypt_failed"

    def test_claim_lost_to_another_worker_does_not_send(self, worker):
        delivery = MagicMock(
            status="pending",
            content_ciphertext=b"x",
            attempt_count=0,
            recommendation_context=_valid_context(),
        )
        db = _claim_db(delivery, target=_active_target(), claim_updated=0)
        row = MagicMock(recommendation_delivery_id="d-1", attempt_count=0)

        with patch(
            "app.services.recommendation_delivery_service.decrypt_delivery_body",
            return_value="正文",
        ):
            assert worker._claim_recommendation_body(db, row, MagicMock()) is None
        assert row.status == "pending"

    @pytest.mark.parametrize(("context", "error_code"), [
        ("{bad-json", "context_parse_failed"),
        ([], "context_not_object"),
        ({}, "context_items_missing"),
        ({"items": "not-a-list"}, "context_items_invalid"),
        ({"items": []}, "context_targets_missing"),
        ({"items": ["not-an-object"]}, "context_item_invalid"),
        ({"items": [{"target_type": "job"}]}, "context_item_invalid"),
        ({"items": [{"target_type": "other", "target_id": 7}]}, "context_item_invalid"),
        ({"items": [{"target_type": "job", "target_id": True}]}, "context_item_invalid"),
    ])
    def test_invalid_context_terminalizes_outbox_and_delivery(
        self, worker, context, error_code,
    ):
        delivery = MagicMock(
            delivery_id="d-1",
            status="pending",
            recommendation_context=context,
            created_at=datetime.now(timezone.utc),
            session_patch_ciphertext=b"patch",
        )
        db = MagicMock()
        db.get.return_value = delivery
        row = MagicMock(
            recommendation_delivery_id="d-1",
            attempt_count=0,
            locked_at="old-lock",
            next_attempt_at="old-due",
        )

        with patch("app.services.worker.log_event") as log:
            assert worker._claim_recommendation_body(db, row, MagicMock()) is None

        assert row.status == "dead_letter"
        assert row.locked_at is None
        assert row.next_attempt_at is None
        assert delivery.status == "permanent_failed"
        assert delivery.last_error == row.last_error
        assert delivery.last_error_code == error_code
        assert delivery.lease_owner is None
        assert delivery.lease_expires_at is None
        assert delivery.session_patch_ciphertext is None
        assert delivery.content_expires_at is not None
        assert db.query.call_count == 1  # delivery lock only; no target was trusted
        log.assert_called_once_with(
            "recommendation_delivery_claim_rejected",
            delivery_id="d-1",
            error_code=error_code,
            severity="alert",
        )

    @pytest.mark.parametrize(("target", "error_code"), [
        (None, "target_missing"),
        (
            SimpleNamespace(
                audit_status="pending",
                deleted_at=None,
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
                delist_reason=None,
            ),
            "target_inactive",
        ),
        (
            SimpleNamespace(
                audit_status="passed",
                deleted_at=None,
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
                delist_reason=None,
            ),
            "target_inactive",
        ),
    ])
    def test_missing_or_inactive_target_terminalizes_both_rows(
        self, worker, target, error_code,
    ):
        delivery = MagicMock(
            delivery_id="d-1",
            status="retry_wait",
            recommendation_context=_valid_context(),
            created_at=datetime.now(timezone.utc),
        )
        db = _claim_db(delivery, target=target)
        row = MagicMock(recommendation_delivery_id="d-1", attempt_count=1)

        with patch("app.services.worker.log_event") as log:
            assert worker._claim_recommendation_body(db, row, MagicMock()) is None

        assert row.status == "dead_letter"
        assert delivery.status == "permanent_failed"
        assert delivery.last_error_code == error_code
        log.assert_called_once()

    def test_valid_json_string_locks_target_and_can_be_claimed(self, worker):
        delivery = MagicMock(
            delivery_id="d-1",
            status="pending",
            content_ciphertext=b"ciphertext",
            attempt_count=0,
            next_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None),
            recommendation_context=json.dumps(_valid_context()),
        )
        db = _claim_db(delivery, target=_active_target(), claim_updated=1)
        row = MagicMock(recommendation_delivery_id="d-1", attempt_count=0)

        with patch(
            "app.services.recommendation_delivery_service.decrypt_delivery_body",
            return_value="正文",
        ):
            assert worker._claim_recommendation_body(db, row, MagicMock()) == "正文"

        assert row.status == "sending"
        assert row.attempt_count == 1

    def test_delivery_lock_refresh_detects_context_change_on_same_identity(self, worker):
        snapshot = worker_module.RecommendationDelivery(
            delivery_id="d-1",
            status="pending",
            recommendation_context={"items": []},
        )
        db = MagicMock()
        db.get.return_value = snapshot

        def refresh_same_identity():
            snapshot.recommendation_context = _valid_context()
            return snapshot

        lock_query = db.query.return_value.populate_existing.return_value
        lock_query.filter.return_value.with_for_update.return_value.first.side_effect = (
            refresh_same_identity
        )
        row = MagicMock(recommendation_delivery_id="d-1", attempt_count=0)

        with patch("app.services.worker.log_event") as log:
            assert worker._claim_recommendation_body(db, row, MagicMock()) is None

        assert row.status == "pending"
        assert snapshot.status == "pending"
        log.assert_not_called()

    def test_target_lock_refresh_detects_inactive_state_on_same_identity(self, worker):
        delivery = MagicMock(
            delivery_id="d-1",
            status="pending",
            recommendation_context=_valid_context(),
            created_at=datetime.now(timezone.utc),
        )
        target = _active_target()
        db = MagicMock()
        db.get.return_value = delivery

        target_query = MagicMock()

        def refresh_same_identity():
            target.audit_status = "rejected"
            return target

        target_query.populate_existing.return_value.filter.return_value.with_for_update.return_value.first.side_effect = (
            refresh_same_identity
        )
        delivery_query = MagicMock()
        delivery_query.populate_existing.return_value.filter.return_value.with_for_update.return_value.first.return_value = MagicMock()
        db.query.side_effect = lambda model: (
            target_query if model is worker_module.Job else delivery_query
        )
        row = MagicMock(recommendation_delivery_id="d-1", attempt_count=0)

        with patch("app.services.worker.log_event") as log:
            assert worker._claim_recommendation_body(db, row, MagicMock()) is None

        assert row.status == "dead_letter"
        assert delivery.status == "permanent_failed"
        assert delivery.last_error_code == "target_inactive"
        target_query.populate_existing.assert_called_once_with()
        log.assert_called_once()


# ---------------------------------------------------------------------------
# P1-15：独立线程 + 派生必须经过 claim
# ---------------------------------------------------------------------------

class TestIndependentRecoveryLoops:
    def test_main_loop_aux_service_no_longer_scans_deliveries(self, worker):
        with patch.object(worker, "_process_rate_limit_notify_once") as notify, \
             patch.object(worker, "_process_send_retry_once") as retry, \
             patch.object(worker, "dispatch_deliveries_once") as dispatch, \
             patch.object(worker, "reconcile_sessions_once") as reconcile, \
             patch.object(worker, "derive_impressions_once") as derive:
            worker._service_aux_queues()

        notify.assert_called_once()
        retry.assert_called_once()
        dispatch.assert_not_called()
        reconcile.assert_not_called()
        derive.assert_not_called()

    def test_loops_use_250ms_interval_and_batch_of_100(self):
        from app.tasks import (
            recommendation_delivery_dispatcher,
            recommendation_impression_deriver,
            recommendation_session_reconciler,
        )

        for module in (
            recommendation_delivery_dispatcher,
            recommendation_session_reconciler,
            recommendation_impression_deriver,
        ):
            assert module.SCAN_INTERVAL_SECONDS == 0.25
            assert module.BATCH_SIZE == 100

        stub = MagicMock()
        recommendation_delivery_dispatcher.run_once(stub)
        stub.dispatch_deliveries_once.assert_called_once_with(limit=100)
        recommendation_session_reconciler.run_once(stub)
        stub.reconcile_sessions_once.assert_called_once_with(limit=100)
        recommendation_impression_deriver.run_once(stub)
        stub.derive_impressions_once.assert_called_once_with(limit=100)

    def test_sent_derivation_goes_through_claim(self, worker):
        """sent 后的即时派生必须经过 claim，不得内联调用 derive_impressions。"""
        claim_db = MagicMock()
        work_db = MagicMock()
        with patch("app.services.worker.SessionLocal", side_effect=[claim_db, work_db]), \
             patch(
                 "app.services.recommendation_exposure_service.claim_impression_deliveries",
                 return_value=["d-1"],
             ) as claim, \
             patch(
                 "app.services.recommendation_exposure_service.derive_impressions",
             ) as derive:
            assert worker.derive_impressions_once(limit=7) == 1

        claim.assert_called_once_with(claim_db, limit=7)
        derive.assert_called_once()

    def test_derivation_never_clears_the_send_lease(self, worker):
        claim_db = MagicMock()
        work_db = MagicMock()
        delivery = MagicMock(lease_owner="worker-1", lease_expires_at="later")
        work_db.get.return_value = delivery
        with patch("app.services.worker.SessionLocal", side_effect=[claim_db, work_db]), \
             patch(
                 "app.services.recommendation_exposure_service.claim_impression_deliveries",
                 return_value=["d-1"],
             ), \
             patch("app.services.recommendation_exposure_service.derive_impressions"):
            worker.derive_impressions_once()

        assert delivery.lease_owner == "worker-1"
        assert delivery.lease_expires_at == "later"

    def test_immediate_submission_is_skipped_without_executor(self, worker):
        with patch.object(worker, "derive_impressions_once") as derive:
            worker._submit_immediate_impressions()
        derive.assert_not_called()


# ---------------------------------------------------------------------------
# P2-9：发送成功后的进程内有限重试
# ---------------------------------------------------------------------------

class TestSendCommitRetry:
    def test_db_outage_retries_in_process_without_resending(self, worker):
        db = MagicMock()
        db.commit.side_effect = [RuntimeError("mysql gone"), None]
        with patch("app.services.worker.SessionLocal", return_value=db), \
             patch("app.services.worker.time.sleep"):
            assert worker._persist_after_send("x", lambda _db: True) is True

        assert db.commit.call_count == 2
        worker._wecom_client.send_text.assert_not_called()

    def test_exhausted_retries_alert_and_keep_sending(self, worker):
        db = MagicMock()
        db.commit.side_effect = RuntimeError("mysql gone")
        with patch("app.services.worker.SessionLocal", return_value=db), \
             patch("app.services.worker.time.sleep"), \
             patch("app.services.worker.log_event") as log:
            assert worker._persist_after_send("x", lambda _db: True) is False

        assert db.commit.call_count == worker_module.SEND_COMMIT_MAX_ATTEMPTS
        assert log.call_args.args[0] == "wecom_send_commit_failed"
        worker._wecom_client.send_text.assert_not_called()


# ---------------------------------------------------------------------------
# P1-17 / P2-12：TTL
# ---------------------------------------------------------------------------

class _RecordingDb:
    def __init__(self, batches: list[list[tuple[str]]]):
        self._batches = batches
        self.statements: list[str] = []

    def execute(self, statement, params=None):  # noqa: ARG002
        sql = str(statement)
        self.statements.append(sql)
        result = MagicMock()
        result.rowcount = 1
        if sql.strip().upper().startswith("SELECT REQUEST_ID"):
            result.fetchall.return_value = (
                self._batches.pop(0) if self._batches else []
            )
        return result

    def commit(self):
        return None


class TestRecommendationTtl:
    def test_sent_is_terminal_and_retry_wait_is_covered(self):
        db = MagicMock()
        ttl_cleanup._redact_expired_recommendation_content(db)
        sql = " ".join(str(call.args[0]) for call in db.execute.call_args_list)

        assert "redacted" not in sql
        assert "'expired'" not in sql
        assert "retry_wait" in sql
        # sent 只清正文，不出现在任何状态改写的 CASE 分支里
        assert "WHEN d.status='sent'" not in sql
        assert "d.content_ciphertext=NULL" in sql
        assert "d.session_patch_ciphertext=NULL" in sql

    def test_detail_purge_follows_the_mandated_fk_order(self):
        db = _RecordingDb([[("r-1",)]])
        ttl_cleanup._purge_expired_recommendation_details(db, 90)

        order = [
            index for index, sql in enumerate(db.statements)
            if any(token in sql for token in (
                "UPDATE `event_log`",
                "DELETE FROM recommendation_impression",
                "DELETE FROM recommendation_delivery",
                "SET served_attempt_id=NULL",
                "DELETE FROM recommendation_search_attempt",
                "DELETE FROM recommendation_request",
            ))
        ]
        touched = [db.statements[i] for i in order]
        assert "UPDATE `event_log`" in touched[0]
        assert "DELETE FROM recommendation_impression" in touched[1]
        assert "DELETE FROM recommendation_delivery" in touched[2]
        assert "SET served_attempt_id=NULL" in touched[3]
        assert "DELETE FROM recommendation_search_attempt" in touched[4]
        assert "DELETE FROM recommendation_request" in touched[5]

    def test_detail_purge_stops_when_nothing_expired(self):
        db = _RecordingDb([[]])
        assert ttl_cleanup._purge_expired_recommendation_details(db, 90) == 0
        assert len(db.statements) == 1


# ---------------------------------------------------------------------------
# scheduler 注册
# ---------------------------------------------------------------------------

def test_scheduler_registers_exposure_reconcile_jobs():
    from app.tasks import scheduler

    # build_scheduler() 只注册不 start，pending job 直接读 id 即可。
    sched = scheduler.build_scheduler()
    ids = {job.id for job in sched.get_jobs()}

    assert "recommendation_exposure_reconcile" in ids
    assert "recommendation_exposure_reconcile_intraday" in ids
    assert "recommendation_privacy_cleanup" in ids

    # §9.11.1：隐私闭环必须排在 ttl_cleanup 之前，否则反查不到候选 target。
    jobs = {job.id: job for job in sched.get_jobs()}

    def _hhmm(job_id: str) -> tuple[int, int]:
        parts = {
            field.name: str(field) for field in jobs[job_id].trigger.fields
        }
        return int(parts["hour"]), int(parts["minute"])

    assert _hhmm("recommendation_privacy_cleanup") < _hhmm("ttl_cleanup")
