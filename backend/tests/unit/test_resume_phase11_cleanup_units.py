"""Phase 11 stage 5 tests, grouped by the three executable units."""
from datetime import datetime, timedelta
from itertools import product
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import Resume
from app.services import recommendation_delivery_service as delivery
from app.tasks import resume_candidate_cleanup, resume_expiry_cleanup


class _ResumeQuery:
    def __init__(self, rows):
        self.rows = rows

    def populate_existing(self): return self
    def filter(self, *_args): return self
    def order_by(self, *_args): return self
    def with_for_update(self): return self
    def all(self): return self.rows


def test_unit_a_locks_resume_ids_in_order_and_rejects_stale_before_writes():
    now = datetime(2026, 8, 18)
    rows = [
        Resume(id=2, audit_status="passed", activated_at=now,
               expires_at=now + timedelta(days=1)),
        Resume(id=5, audit_status="passed", activated_at=now,
               expires_at=now + timedelta(days=1)),
    ]
    db = MagicMock()
    db.query.return_value = _ResumeQuery(rows)
    ctx = {"items": [
        {"target_type": "resume", "target_id": 5},
        {"target_type": "resume", "target_id": 2},
    ]}
    assert delivery.lock_and_validate_recommendation_targets(
        db, ctx=ctx, fact={}, now=now,
    ) == [2, 5]
    rows[1].delist_reason = "expired"
    with pytest.raises(delivery.RecommendationTargetStale) as exc:
        delivery.lock_and_validate_recommendation_targets(
            db, ctx=ctx, fact={}, now=now,
        )
    assert exc.value.code == "recommendation_target_stale"


def test_unit_a_fact_only_search_worker_locks_persisted_candidate_ids():
    assert delivery._resume_target_ids({}, {
        "direction": "search_worker",
        "candidate_ids": ["9", "3", "9"],
        "served_top_ids": [3],
    }) == [3, 9]


def test_unit_b_expiry_only_creates_task_after_conditional_update(monkeypatch):
    now = datetime(2026, 8, 18)
    db = MagicMock()
    selected = MagicMock()
    selected.fetchall.return_value = [(7,), (8,)]
    missed = MagicMock(rowcount=0)
    changed = MagicMock(rowcount=1)
    db.execute.side_effect = [selected, changed, missed]
    ensure = MagicMock()
    monkeypatch.setattr(resume_expiry_cleanup, "ensure_target_cleanup_task", ensure)
    assert resume_expiry_cleanup.expire_locked_batch(db, now=now) == [7]
    ensure.assert_called_once_with(db, "resume", 7, reason="expired")
    sql = str(db.execute.call_args_list[0].args[0])
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "candidate_expires_at IS NULL" in sql


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_unit_b_candidate_predicate_never_accepts_an_active_resume(status):
    now = datetime(2026, 8, 18)
    candidate = Resume(
        audit_status=status, activated_at=None, expires_at=None,
        candidate_expires_at=now, deleted_at=None,
    )
    assert resume_candidate_cleanup._is_due(candidate, now)
    candidate.expires_at = now + timedelta(days=30)
    assert not resume_candidate_cleanup._is_due(candidate, now)


def test_unit_b_uses_one_now_for_every_expiry_batch(monkeypatch):
    db = MagicMock()
    moments = []
    batches = [[1], [2], []]
    monkeypatch.setattr(
        resume_expiry_cleanup, "expire_locked_batch",
        lambda _db, *, now, batch_size: moments.append(now) or batches.pop(0),
    )
    fixed = datetime(2026, 8, 18, 1, 2, 3)
    result = resume_expiry_cleanup.process_expired_resumes(
        db, now=fixed, max_runtime_seconds=None,
    )
    assert moments == [fixed, fixed, fixed]
    assert result["processed"] == 2


@pytest.mark.parametrize(
    "module,setting_name",
    [
        (resume_expiry_cleanup, "resume_expiry_cleanup_enabled"),
        (resume_candidate_cleanup, "resume_candidate_cleanup_enabled"),
    ],
)
def test_unit_b_disabled_switch_takes_no_lock_or_database(
    monkeypatch, module, setting_name,
):
    from app.config import settings
    monkeypatch.setattr(settings, setting_name, False)
    lock = MagicMock(side_effect=AssertionError("must not lock"))
    session = MagicMock(side_effect=AssertionError("must not open database"))
    monkeypatch.setattr(module, "renewable_task_lock", lock)
    monkeypatch.setattr(module, "SessionLocal", session)
    module.run()
    lock.assert_not_called()
    session.assert_not_called()


def test_unit_b_scheduler_registers_both_ten_minute_resume_jobs():
    from app.tasks.scheduler import build_scheduler
    sched = build_scheduler()
    expiry = sched.get_job("resume_expiry_cleanup")
    candidate = sched.get_job("resume_candidate_cleanup")
    assert expiry is not None and candidate is not None
    assert expiry.trigger.interval.total_seconds() == 600
    assert candidate.trigger.interval.total_seconds() == 600


def test_unit_c_target_cleanup_worker_contract_is_resume_generic():
    from app.services import target_cleanup_service
    assert target_cleanup_service._target_model("resume") is Resume
    task = SimpleNamespace(status="succeeded")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = task
    assert target_cleanup_service.target_cleanup_succeeded(db, "resume", 9)


@pytest.mark.parametrize("flags", list(product((False, True), repeat=5)))
def test_five_switch_matrix_keeps_cleanup_gates_independent(monkeypatch, flags):
    """32-case contract: unrelated rollout flags never open cleanup switches."""
    from app.config import settings
    from app.tasks import ttl_cleanup

    names = (
        "resume_lifecycle_v2_enabled", "resume_replacement_enabled",
        "resume_expiry_cleanup_enabled", "resume_candidate_cleanup_enabled",
        "resume_hard_delete_enabled",
    )
    for name, value in zip(names, flags):
        monkeypatch.setattr(settings, name, value)

    # Enabled paths already have focused behavior tests above.  This matrix's
    # executable contract is fail-closed independence: no other bit may turn a
    # disabled cleanup path into a lock or write.
    if not flags[2]:
        lock = MagicMock(side_effect=AssertionError("expiry lock opened"))
        monkeypatch.setattr(resume_expiry_cleanup, "renewable_task_lock", lock)
        resume_expiry_cleanup.run()
        lock.assert_not_called()
    if not flags[3]:
        lock = MagicMock(side_effect=AssertionError("candidate lock opened"))
        monkeypatch.setattr(resume_candidate_cleanup, "renewable_task_lock", lock)
        resume_candidate_cleanup.run()
        lock.assert_not_called()
    if not flags[4]:
        db = MagicMock()
        assert ttl_cleanup._hard_delete_expired_resumes(db, 7) == 0
        db.execute.assert_not_called()
