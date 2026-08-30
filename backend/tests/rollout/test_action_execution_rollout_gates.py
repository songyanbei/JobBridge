"""C1 rollout gates and observation stop conditions."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.tasks import worker_monitor
from scripts import action_contact_chaos
from scripts import action_execution_preflight
from scripts import legacy_exit_gate


def _lock(acquired: bool = True):
    cm = MagicMock()
    cm.__enter__.return_value = acquired
    cm.__exit__.return_value = False
    return cm


def test_action_monitor_alerts_and_engages_kill_switch(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter.return_value.scalar.side_effect = [1, 301, 1, 2, 601]
    redis = MagicMock()
    monkeypatch.setattr(worker_monitor, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker_monitor, "get_redis", lambda: redis)
    with patch.object(worker_monitor, "task_lock", return_value=_lock()), \
         patch.object(worker_monitor, "_alert") as alert:
        worker_monitor.check_action_execution()

    assert [call.args[0] for call in alert.call_args_list] == [
        "action_stale_lease",
        "action_missing_reference",
        "action_replay_backlog",
    ]
    assert redis.set.call_count == 3
    db.close.assert_called_once()


def test_action_monitor_healthy_is_read_only(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter.return_value.scalar.side_effect = [0, 0, 0, 0, 0]
    monkeypatch.setattr(worker_monitor, "SessionLocal", lambda: db)
    with patch.object(worker_monitor, "task_lock", return_value=_lock()), \
         patch.object(worker_monitor, "_alert") as alert, \
         patch.object(worker_monitor, "_stop_action_routing") as stop:
        worker_monitor.check_action_execution()
    alert.assert_not_called()
    stop.assert_not_called()


def test_preflight_defaults_fail_closed_without_database(monkeypatch):
    for name in (
        "ACTION_EXECUTION_MODE",
        "ACTION_EXECUTION_ROLLOUT_PERCENTAGE",
        "ACTION_EXECUTION_LEASE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert action_execution_preflight.run(dsn=None) == 0


def test_preflight_rejects_invalid_mode_or_rollout(monkeypatch):
    monkeypatch.setenv("ACTION_EXECUTION_MODE", "on")
    monkeypatch.setenv("ACTION_EXECUTION_ROLLOUT_PERCENTAGE", "101")
    assert action_execution_preflight.run(dsn=None) == 2


def test_c2_chaos_matrix_covers_all_rows_and_forbidden_outcomes():
    report = action_contact_chaos.run()
    assert report["scenario_count"] == 9
    assert report["passed"] is True
    assert all(item["forbidden_hits"] == [] for item in report["results"])


def test_c2_chaos_scenario_is_repeatable_and_selectable():
    first = action_contact_chaos.run(["grant_consumed_provider_timeout"])
    second = action_contact_chaos.run(["grant_consumed_provider_timeout"])
    assert first["passed"] is True
    assert second["passed"] is True
    assert first["results"][0]["events"] == second["results"][0]["events"]


def _legacy_evidence(days: int = 14) -> dict[str, object]:
    return {
        "action_on_coverage": [99.5] * days,
        "replay_recovery_success_rate": [99.95] * days,
        "duplicate_provider_calls": [0] * days,
        "contact_pii_leaks": [0] * days,
        "contact_token_replays": [0] * days,
        "golden_diffs_approved": True,
        "legacy_compatibility": True,
        "pending_action_count": 0,
        "pending_session_count": 0,
        "pending_outbox_count": 0,
        "rollback_drill_passed": True,
    }


def test_c3_legacy_exit_requires_full_fourteen_day_evidence():
    report = legacy_exit_gate.evaluate(_legacy_evidence())
    assert report["eligible_to_propose_rfc"] is True

    evidence = _legacy_evidence()
    evidence["action_on_coverage"] = [99.5] * 13 + [98.9]
    blocked = legacy_exit_gate.evaluate(evidence)
    assert blocked["eligible_to_propose_rfc"] is False
    assert any(item["code"] == "action_coverage" and not item["passed"] for item in blocked["findings"])


def test_c3_legacy_exit_blocks_unapproved_diffs_and_pending_facts():
    evidence = _legacy_evidence()
    evidence["golden_diffs_approved"] = False
    evidence["pending_outbox_count"] = 1
    report = legacy_exit_gate.evaluate(evidence)
    assert report["eligible_to_propose_rfc"] is False
    blocked_codes = {item["code"] for item in report["findings"] if not item["passed"]}
    assert {"golden_diffs_approved", "pending_outbox_count"} <= blocked_codes
