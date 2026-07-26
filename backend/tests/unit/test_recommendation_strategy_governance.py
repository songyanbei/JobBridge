"""策略治理 / kill switch 分发 / RBAC / 模拟 的单元测试。

对应方案 §7（生命周期、灰度、总开关）、§8（模拟）、§9.3/§9.3.1、§9.10、
§11.7、§14.7、§14.8。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.api import deps
from app.core.exceptions import BusinessException
from app.schemas.recommendation import RecommendationItem, RecommendationScoreDetail
from app.services import admin_user_service
from app.services import recommendation_strategy_service as svc


# ---------------------------------------------------------------------------
# §9.10 / §14.8 RBAC
# ---------------------------------------------------------------------------

class TestRoleResolution:
    @pytest.mark.parametrize("raw", [None, "", "root", "SUPER_ADMIN ", 123, object()])
    def test_unknown_role_falls_back_to_least_privilege(self, raw):
        if raw == "SUPER_ADMIN ":
            # 大小写/空白仍然是合法角色，单独断言。
            assert admin_user_service.normalize_role(raw) == "super_admin"
            return
        assert admin_user_service.normalize_role(raw) == "viewer"

    def test_missing_role_attribute_is_not_super_admin(self):
        assert admin_user_service.resolve_role(SimpleNamespace()) == "viewer"

    def test_mock_object_does_not_become_super_admin(self):
        """MagicMock 的 role 属性是 MagicMock，旧实现会把它当成 super_admin。"""
        assert admin_user_service.resolve_role(MagicMock()) == "viewer"

    def test_permission_matrix_matches_plan(self):
        viewer = SimpleNamespace(role="viewer")
        operator = SimpleNamespace(role="operator")
        superadmin = SimpleNamespace(role="super_admin")
        assert admin_user_service.has_permission(viewer, "strategy_simulate")
        assert not admin_user_service.has_permission(viewer, "strategy_draft_edit")
        assert admin_user_service.has_permission(operator, "strategy_draft_edit")
        assert not admin_user_service.has_permission(operator, "strategy_publish")
        for capability in (
            "strategy_publish", "strategy_rollout", "strategy_promote",
            "strategy_rollback", "strategy_kill_switch",
        ):
            assert admin_user_service.has_permission(superadmin, capability)
            assert not admin_user_service.has_permission(operator, capability)
            assert not admin_user_service.has_permission(viewer, capability)

    def test_role_at_least_orders_roles(self):
        assert admin_user_service.role_at_least(SimpleNamespace(role="super_admin"), "operator")
        assert not admin_user_service.role_at_least(SimpleNamespace(role="viewer"), "operator")


class TestRequireAdminRole:
    def test_viewer_rejected_from_super_admin_route(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "admin_force_password_change", False)
        dependency = deps.require_admin_role("super_admin")
        with pytest.raises(BusinessException) as excinfo:
            dependency(current=SimpleNamespace(role="viewer", password_changed=1))
        assert excinfo.value.code == 40301

    def test_super_admin_allowed(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "admin_force_password_change", False)
        dependency = deps.require_admin_role("super_admin")
        admin = SimpleNamespace(role="super_admin", password_changed=1)
        assert dependency(current=admin) is admin

    def test_existing_super_admin_keeps_every_console_route(self, monkeypatch):
        """回归：fail-closed 之后存量超级管理员不能被锁在门外。"""
        monkeypatch.setattr(deps.settings, "admin_force_password_change", False)
        admin = SimpleNamespace(role="super_admin", password_changed=1)
        for roles in (
            ("viewer", "operator", "super_admin"),
            ("operator", "super_admin"),
            ("super_admin",),
        ):
            assert deps.require_admin_role(*roles)(current=admin) is admin

    def test_role_missing_is_denied_not_granted(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "admin_force_password_change", False)
        dependency = deps.require_admin_role("operator", "super_admin")
        with pytest.raises(BusinessException) as excinfo:
            dependency(current=SimpleNamespace(password_changed=1))
        assert excinfo.value.code == 40301

    def test_typo_in_route_declaration_fails_loudly(self):
        with pytest.raises(ValueError):
            deps.require_admin_role("superadmin")
        with pytest.raises(ValueError):
            deps.require_admin_role()

    def test_permission_dependency(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "admin_force_password_change", False)
        dependency = deps.require_admin_permission("strategy_kill_switch")
        with pytest.raises(BusinessException):
            dependency(current=SimpleNamespace(role="operator", password_changed=1))


# ---------------------------------------------------------------------------
# §7.5 动态总开关分发
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_runtime_control():
    svc.reset_runtime_control_cache()
    yield
    svc.reset_runtime_control_cache()


def _control_row(kill: bool, revision: int):
    return SimpleNamespace(kill_switch=kill, revision=revision)


def _db_with_control(row):
    db = MagicMock()
    db.get.return_value = row
    return db


class TestRuntimeControlDistribution:
    def test_revision_must_not_go_backwards(self):
        svc.apply_runtime_control_update({"kill_switch": True, "revision": 5})
        svc.apply_runtime_control_update({"kill_switch": False, "revision": 3})
        state = svc.runtime_control_state()
        assert (state.kill_switch, state.revision) == (True, 5)

    def test_equal_revision_is_accepted(self):
        svc.apply_runtime_control_update({"kill_switch": False, "revision": 7})
        svc.apply_runtime_control_update({"kill_switch": True, "revision": 7})
        assert svc.runtime_control_state().kill_switch is True

    def test_malformed_payload_is_ignored(self):
        svc.apply_runtime_control_update({"kill_switch": True, "revision": 2})
        assert svc.apply_runtime_control_update({"revision": 9}) is None
        assert svc.apply_runtime_control_update("not-a-dict") is None
        assert svc.runtime_control_state().revision == 2

    def test_string_booleans_from_redis_are_parsed(self):
        svc.apply_runtime_control_update({"kill_switch": "true", "revision": 1})
        assert svc.runtime_control_state().kill_switch is True

    def test_fresh_local_value_is_served_without_resource_access(self, monkeypatch):
        svc.apply_runtime_control_update({"kill_switch": True, "revision": 4}, source="db")
        db = MagicMock()
        monkeypatch.setattr(
            svc.redis_client, "read_runtime_control",
            lambda: pytest.fail("must not hit redis while local value is fresh"),
        )
        state = svc.resolve_runtime_control(db)
        assert state.kill_switch is True
        db.get.assert_not_called()

    def test_stale_local_value_is_resourced_from_db(self, monkeypatch):
        svc.apply_runtime_control_update({"kill_switch": True, "revision": 4}, source="db")
        stale = svc.runtime_control_state()
        monkeypatch.setattr(
            svc, "_control_state",
            svc.RuntimeControlState(
                kill_switch=stale.kill_switch, revision=stale.revision,
                source="db", checked_at=time.monotonic() - 60,
            ),
        )
        db = _db_with_control(_control_row(False, 5))
        state = svc.resolve_runtime_control(db)
        assert (state.kill_switch, state.revision, state.source) == (False, 5, "db")

    def test_redis_down_but_db_available_reads_db(self, monkeypatch):
        monkeypatch.setattr(svc.redis_client, "read_runtime_control", lambda: None)
        db = _db_with_control(_control_row(True, 3))
        assert svc.resolve_runtime_control(db).kill_switch is True

    def test_db_down_falls_back_to_redis(self, monkeypatch):
        db = MagicMock()
        db.get.side_effect = RuntimeError("db down")
        monkeypatch.setattr(
            svc.redis_client, "read_runtime_control",
            lambda: {"kill_switch": False, "revision": 11},
        )
        state = svc.resolve_runtime_control(db)
        assert (state.kill_switch, state.source) == (False, "redis")

    def test_both_unavailable_fails_safe_to_kill(self, monkeypatch):
        db = MagicMock()
        db.get.side_effect = RuntimeError("db down")
        monkeypatch.setattr(svc.redis_client, "read_runtime_control", lambda: None)
        state = svc.resolve_runtime_control(db)
        assert state.kill_switch is True
        assert state.source == "fail_safe"
        assert state.verified is False

    def test_fail_safe_state_is_not_latched(self, monkeypatch):
        """fail-safe 只影响当次请求，不能把进程钉死在假 revision 上。"""
        svc.apply_runtime_control_update({"kill_switch": False, "revision": 2}, source="db")
        monkeypatch.setattr(
            svc, "_control_state",
            svc.RuntimeControlState(False, 2, "db", time.monotonic() - 60),
        )
        monkeypatch.setattr(svc.redis_client, "read_runtime_control", lambda: None)
        db = MagicMock()
        db.get.side_effect = RuntimeError("db down")
        assert svc.resolve_runtime_control(db).kill_switch is True
        assert svc.runtime_control_state().revision == 2

    def test_no_local_value_and_no_backend_fails_safe(self, monkeypatch):
        monkeypatch.setattr(svc.redis_client, "read_runtime_control", lambda: None)
        assert svc.resolve_runtime_control(None).kill_switch is True

    def test_env_override_true_forces_kill(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "recommendation_strategy_kill_switch", True, raising=False)
        db = _db_with_control(_control_row(False, 9))
        state = svc.resolve_runtime_control(db)
        assert (state.kill_switch, state.source) == (True, "env")

    def test_env_false_never_overrides_db_true(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "recommendation_strategy_kill_switch", False, raising=False)
        db = _db_with_control(_control_row(True, 9))
        assert svc.resolve_runtime_control(db).kill_switch is True

    def test_env_override_is_latched_at_process_start(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "recommendation_strategy_kill_switch", True, raising=False)
        assert svc.env_kill_switch_override() is True
        monkeypatch.setattr(settings, "recommendation_strategy_kill_switch", False, raising=False)
        # 需要滚动重启才能解除，不能在线秒级放开。
        assert svc.env_kill_switch_override() is True

    def test_broadcast_updates_local_value_even_when_redis_fails(self, monkeypatch):
        monkeypatch.setattr(svc.redis_client, "publish_runtime_control", lambda payload: False)
        assert svc.broadcast_runtime_control(kill_switch=True, revision=6) is False
        state = svc.runtime_control_state()
        assert (state.kill_switch, state.revision) == (True, 6)

    def test_broadcast_payload_carries_revision(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            svc.redis_client, "publish_runtime_control",
            lambda payload: captured.update(payload) or True,
        )
        svc.broadcast_runtime_control(kill_switch=False, revision=12)
        assert captured["revision"] == 12
        assert captured["kill_switch"] is False

    def test_db_poll_refreshes_local_value(self):
        db = _db_with_control(_control_row(True, 8))
        svc._poll_runtime_control_once(lambda: db)
        assert svc.runtime_control_state().revision == 8
        db.close.assert_called_once()

    def test_runtime_control_ttl_and_poll_budget(self):
        assert svc.RUNTIME_CONTROL_MAX_AGE_SECONDS == 5.0
        assert svc.RUNTIME_CONTROL_POLL_SECONDS == 5.0
        from app.core import redis_client

        assert redis_client.RUNTIME_CONTROL_TTL_SECONDS == 30
        assert redis_client.RUNTIME_CONTROL_KEY == "recommendation:runtime_control"


# ---------------------------------------------------------------------------
# §7.2 / §7.5 分配
# ---------------------------------------------------------------------------

def _release(mode, *, stable=None, candidate=None, rollout=100):
    return SimpleNamespace(
        execution_mode=mode, stable_version_id=stable,
        candidate_version_id=candidate, rollout_percentage=rollout,
        revision=1, direction="search_job",
    )


class TestAssignment:
    def test_kill_switch_forces_legacy(self):
        release = _release("on", stable=7, candidate=9)
        assert svc.select_assignment(
            release=release, userid="u1", direction="search_job", kill_switch=True,
        ) == ("legacy", None)

    def test_kill_switch_stops_shadow_double_compute(self):
        release = _release("shadow", candidate=9)
        assert svc.shadow_candidate_version_id(
            release=release, userid="u1", direction="search_job", kill_switch=True,
        ) is None

    def test_shadow_serves_stable_not_legacy(self):
        """P2-24：shadow 下用户侧统一走 stable，不能同一模式两种体验。"""
        release = _release("shadow", stable=7, candidate=9)
        assert svc.select_assignment(
            release=release, userid="u1", direction="search_job",
        ) == ("stable", 7)

    def test_shadow_without_stable_is_legacy(self):
        release = _release("shadow", stable=None, candidate=9)
        assert svc.select_assignment(
            release=release, userid="u1", direction="search_job",
        ) == ("legacy", None)

    def test_shadow_exposes_candidate_for_offline_diff(self):
        release = _release("shadow", stable=7, candidate=9, rollout=100)
        assert svc.shadow_candidate_version_id(
            release=release, userid="u1", direction="search_job",
        ) == 9

    def test_shadow_zero_percent_never_runs(self):
        release = _release("shadow", stable=7, candidate=9, rollout=0)
        assert svc.shadow_candidate_version_id(
            release=release, userid="u1", direction="search_job",
        ) is None

    def test_on_mode_has_no_shadow_work(self):
        release = _release("on", stable=7, candidate=9)
        assert svc.shadow_candidate_version_id(
            release=release, userid="u1", direction="search_job",
        ) is None

    def test_on_mode_bucket_hit_uses_candidate(self):
        release = _release("on", stable=7, candidate=9, rollout=100)
        assert svc.select_assignment(
            release=release, userid="u1", direction="search_job",
        ) == ("candidate", 9)

    def test_on_mode_bucket_miss_uses_stable(self):
        release = _release("on", stable=7, candidate=9, rollout=0)
        assert svc.select_assignment(
            release=release, userid="u1", direction="search_job",
        ) == ("stable", 7)

    def test_off_mode_is_legacy(self):
        release = _release("off", stable=7, candidate=9)
        assert svc.select_assignment(
            release=release, userid="u1", direction="search_job",
        ) == ("legacy", None)

    def test_missing_release_is_legacy(self):
        assert svc.select_assignment(
            release=None, userid="u1", direction="search_job",
        ) == ("legacy", None)

    def test_same_user_direction_is_stable_across_calls(self):
        release = _release("on", stable=7, candidate=9, rollout=50)
        first = svc.select_assignment(release=release, userid="u1", direction="search_job")
        for _ in range(5):
            assert svc.select_assignment(
                release=release, userid="u1", direction="search_job",
            ) == first


class TestSnapshotInvalidation:
    def test_v1_snapshot_dropped_when_killed(self):
        assert svc.snapshot_is_invalidated_by_kill_switch(
            algorithm_version="recommendation-v1", kill_switch=True,
        ) is True

    def test_legacy_snapshot_survives_kill(self):
        assert svc.snapshot_is_invalidated_by_kill_switch(
            algorithm_version="legacy", kill_switch=True,
        ) is False

    def test_nothing_dropped_while_not_killed(self):
        assert svc.snapshot_is_invalidated_by_kill_switch(
            algorithm_version="recommendation-v1", kill_switch=False,
        ) is False


# ---------------------------------------------------------------------------
# §9.3 不可变历史
# ---------------------------------------------------------------------------

class TestReleaseHistoryConstraints:
    def _kwargs(self, **overrides):
        base = dict(
            direction="search_job", revision=2, operation="mode_change",
            execution_mode="on", stable_version_id=None, candidate_version_id=3,
            rollout_percentage=5, change_reason="why", created_by="admin",
        )
        base.update(overrides)
        return base

    def test_rollback_requires_target_revision(self):
        with pytest.raises(svc.ReleaseStateError):
            svc.append_release_history(MagicMock(), **self._kwargs(operation="rollback"))

    def test_non_rollback_must_not_carry_target_revision(self):
        with pytest.raises(svc.ReleaseStateError):
            svc.append_release_history(MagicMock(), **self._kwargs(target_revision=1))

    def test_unknown_operation_rejected(self):
        with pytest.raises(svc.ReleaseStateError):
            svc.append_release_history(MagicMock(), **self._kwargs(operation="whatever"))

    def test_valid_rollback_is_inserted(self):
        db = MagicMock()
        svc.append_release_history(db, **self._kwargs(operation="rollback", target_revision=1))
        db.add.assert_called_once()

    def test_operation_vocabulary_matches_plan(self):
        assert svc.RELEASE_OPERATIONS == (
            "init", "publish_candidate", "mode_change", "rollout", "promote", "rollback",
        )

    def test_rollout_only_change_is_recorded_as_rollout(self):
        before = {"execution_mode": "on", "rollout_percentage": 5}
        assert svc._history_operation(before, "on", 25) == "rollout"

    def test_mode_switch_is_recorded_as_mode_change(self):
        before = {"execution_mode": "shadow", "rollout_percentage": 100}
        assert svc._history_operation(before, "on", 5) == "mode_change"


class TestDirectionValidation:
    @pytest.mark.parametrize("bad", ["", "search", "search_jobs", "SEARCH_JOB", None])
    def test_unknown_direction_rejected(self, bad):
        with pytest.raises(svc.ReleaseStateError):
            svc.validate_direction(bad)

    def test_known_directions_accepted(self):
        for direction in ("search_job", "search_worker"):
            assert svc.validate_direction(direction) == direction


# ---------------------------------------------------------------------------
# §7.1 / §7.3 promote 与归档
# ---------------------------------------------------------------------------

def _promote_db(release, candidate, *, update_rows=1):
    db = MagicMock()

    def _get(model, key):
        from app.models import RecommendationStrategyRelease, RecommendationStrategyVersion

        if model is RecommendationStrategyRelease:
            return release
        if model is RecommendationStrategyVersion:
            return candidate
        return None

    db.get.side_effect = _get
    db.query.return_value.filter.return_value.update.return_value = update_rows
    return db


class TestPromote:
    def _release(self, **overrides):
        base = dict(
            direction="search_job", execution_mode="on", stable_version_id=4,
            candidate_version_id=9, rollout_percentage=100, revision=6,
            lock_version=3, updated_by="admin", updated_at=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _candidate(self):
        return SimpleNamespace(id=9, status="published", direction="search_job")

    def test_requires_on_at_full_rollout(self):
        db = _promote_db(self._release(rollout_percentage=50), self._candidate())
        with pytest.raises(svc.ReleaseStateError, match="rollout_percentage=100"):
            svc.promote_release(
                db, direction="search_job", lock_version=3,
                change_reason="go", operator="admin",
            )

    def test_shadow_cannot_be_promoted(self):
        db = _promote_db(self._release(execution_mode="shadow"), self._candidate())
        with pytest.raises(svc.ReleaseStateError):
            svc.promote_release(
                db, direction="search_job", lock_version=3,
                change_reason="go", operator="admin",
            )

    def test_no_candidate_cannot_be_promoted(self):
        db = _promote_db(self._release(candidate_version_id=None), None)
        with pytest.raises(svc.ReleaseStateError):
            svc.promote_release(
                db, direction="search_job", lock_version=3,
                change_reason="go", operator="admin",
            )

    def test_promote_archives_previous_stable(self):
        release = self._release()
        db = _promote_db(release, self._candidate())
        _row, before, archived = svc.promote_release(
            db, direction="search_job", lock_version=3,
            change_reason="go", operator="admin",
        )
        assert before["candidate_version_id"] == 9
        assert archived == 4
        update_calls = db.query.return_value.filter.return_value.update.call_args_list
        # 一次 release CAS + 一次旧 stable 归档
        assert any(call.args[0].get("status") == "archived" for call in update_calls)
        assert any(call.args[0].get("candidate_version_id") is None for call in update_calls)

    def test_first_promote_without_previous_stable_archives_nothing(self):
        release = self._release(stable_version_id=None)
        db = _promote_db(release, self._candidate())
        _row, _before, archived = svc.promote_release(
            db, direction="search_job", lock_version=3,
            change_reason="go", operator="admin",
        )
        assert archived is None

    def test_lock_conflict_is_raised(self):
        db = _promote_db(self._release(), self._candidate(), update_rows=0)
        with pytest.raises(svc.ReleaseLockConflict):
            svc.promote_release(
                db, direction="search_job", lock_version=99,
                change_reason="go", operator="admin",
            )


class TestOptimisticLocking:
    def test_release_update_uses_cas_predicate(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.update.return_value = 1
        row = SimpleNamespace(direction="search_job", revision=1, lock_version=4)
        svc._cas_release(db, row, expected_lock_version=4, values={"updated_by": "a"})
        values = db.query.return_value.filter.return_value.update.call_args.args[0]
        assert "revision" in values and "lock_version" in values
        # filter 必须带 lock_version 谓词，否则并发会丢更新
        assert db.query.return_value.filter.call_count == 1

    def test_stale_lock_version_raises(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.update.return_value = 0
        row = SimpleNamespace(direction="search_job", revision=1, lock_version=4)
        with pytest.raises(svc.ReleaseLockConflict):
            svc._cas_release(db, row, expected_lock_version=1, values={})


class TestKillSwitchPersistence:
    def test_cas_conflict_raises(self):
        db = MagicMock()
        db.get.return_value = SimpleNamespace(
            scope="global", kill_switch=False, revision=2, lock_version=2,
            change_reason="x", updated_by="a",
        )
        db.query.return_value.filter.return_value.update.return_value = 0
        with pytest.raises(svc.ReleaseLockConflict):
            svc.set_kill_switch(
                db, enabled=True, lock_version=1, change_reason="incident", operator="admin",
            )

    def test_before_snapshot_is_captured(self):
        db = MagicMock()
        db.get.return_value = SimpleNamespace(
            scope="global", kill_switch=False, revision=2, lock_version=2,
            change_reason="x", updated_by="a",
        )
        db.query.return_value.filter.return_value.update.return_value = 1
        _row, before = svc.set_kill_switch(
            db, enabled=True, lock_version=2, change_reason="incident", operator="admin",
        )
        assert before == {
            "kill_switch": False, "revision": 2, "lock_version": 2,
            "change_reason": "x", "updated_by": "a",
        }


# ---------------------------------------------------------------------------
# §8 模拟
# ---------------------------------------------------------------------------

def _item(target_id, position, *, match=0.5, exploration=False, codes=None):
    return RecommendationItem(
        target_type="job", target_id=target_id, position=position,
        final_score=0.5, is_exploration=exploration, reason_codes=list(codes or []),
        score_detail=RecommendationScoreDetail(
            match_score=match, quality_score=0.5, freshness_score=0.5,
            exposure_opportunity=0.5, base_score=match, repeat_factor=1.0,
            repeat_adjusted_score=match, is_exploration=exploration,
        ),
    )


class TestSimulationReasonCodes:
    def test_rank_up_and_down_are_reported(self):
        from app.api.admin.recommendation_strategies import rank_change_reasons

        current = [_item(1, 1), _item(2, 2)]
        draft = [_item(2, 1), _item(1, 2)]
        changes = {row["target_id"]: row for row in rank_change_reasons(current, draft)}
        assert changes[2]["movement"] == "up"
        assert "rank_up" in changes[2]["reason_codes"]
        assert changes[1]["movement"] == "down"

    def test_entering_and_leaving_top_n(self):
        from app.api.admin.recommendation_strategies import rank_change_reasons

        changes = {
            row["target_id"]: row
            for row in rank_change_reasons([_item(1, 1)], [_item(5, 1)])
        }
        assert changes[5]["reason_codes"] == ["entered_top_n"]
        assert changes[1]["reason_codes"] == ["left_top_n"]
        assert changes[1]["draft_position"] is None

    def test_component_drivers_are_explained(self):
        from app.api.admin.recommendation_strategies import rank_change_reasons

        current = [_item(1, 1, match=0.4), _item(2, 2, match=0.9)]
        draft = [_item(1, 1, match=0.8), _item(2, 2, match=0.9)]
        changes = {row["target_id"]: row for row in rank_change_reasons(current, draft)}
        assert "match_up" in changes[1]["reason_codes"]
        assert "base_score_up" in changes[1]["reason_codes"]
        assert changes[2]["reason_codes"] == []

    def test_exploration_slot_is_reported(self):
        from app.api.admin.recommendation_strategies import rank_change_reasons

        current = [_item(1, 1)]
        draft = [_item(1, 1, exploration=True)]
        codes = rank_change_reasons(current, draft)[0]["reason_codes"]
        assert "exploration_slot_gained" in codes


class TestSimulationLegacyBaseline:
    def test_null_stable_produces_legacy_comparison(self):
        from app.api.admin.recommendation_strategies import _legacy_baseline_items

        candidates = [{"id": 3, "owner_userid": "o1"}, {"id": 5, "owner_userid": "o2"}]
        items = _legacy_baseline_items(candidates, "search_job", 3)
        assert [item.target_id for item in items] == [3, 5]
        assert [item.position for item in items] == [1, 2]
        assert all(item.reason_codes == ["legacy_baseline"] for item in items)

    def test_worker_direction_yields_resume_items(self):
        from app.api.admin.recommendation_strategies import _legacy_baseline_items

        items = _legacy_baseline_items([{"id": 1}], "search_worker", 3)
        assert items[0].target_type == "resume"


def _job_candidate(cid, owner):
    return {
        "id": cid, "city": "深圳", "district": "宝安", "job_category": "普工",
        "salary_floor_monthly": 6000, "salary_ceiling_monthly": 8000,
        "pay_type": "monthly", "headcount": 5, "gender_required": "any",
        "is_long_term": 1, "provide_meal": 1, "provide_housing": 1,
        "shift_pattern": "day", "work_hours": "8h", "description": "描述",
        "created_at": "2026-07-20 10:00:00", "owner_userid": owner,
        "employment_type": "long_term", "accept_couple": 1,
        "accept_student": 0, "accept_minority": 1, "company": "A厂",
        "contact_person": "王", "phone": "13800000000",
    }


def _simulation_db(draft, release, stable=None):
    from app.models import RecommendationStrategyRelease, RecommendationStrategyVersion

    db = MagicMock()

    def _get(model, key):
        if model is RecommendationStrategyRelease:
            return release
        if model is RecommendationStrategyVersion:
            return stable if (stable is not None and key == stable.id) else draft
        return None

    db.get.side_effect = _get
    return db


class TestSimulationEndpoint:
    def _draft(self):
        from app.schemas.recommendation import RecommendationStrategyParameters

        params = RecommendationStrategyParameters.from_template("balanced").model_dump(mode="json")
        return SimpleNamespace(
            id=11, direction="search_job", status="draft", parameters=params,
            parameters_digest="digest-draft", last_simulated_digest=None,
            last_simulated_at=None,
        )

    def _patch_search(self, monkeypatch, candidates):
        from app.services import search_service

        monkeypatch.setattr(search_service, "_query_jobs", lambda criteria, limit, db: [])
        monkeypatch.setattr(search_service, "_jobs_to_dicts", lambda rows, db: candidates)
        monkeypatch.setattr(
            search_service.conversation_service, "compute_query_digest", lambda criteria: "digest-1",
        )

    def _patch_exposures(self, monkeypatch, calls):
        from app.services import recommendation_exposure_service as exposure

        def _counts(db, *, target_type, candidate_ids, request_now_utc, **kwargs):
            calls["counts"] = list(candidate_ids)
            return {cid: index for index, cid in enumerate(candidate_ids)}

        def _recent(db, *, viewer_userid, target_type, candidate_ids, request_now_utc, cooldown_hours):
            calls["recent"] = (viewer_userid, cooldown_hours)
            return {}

        monkeypatch.setattr(exposure, "batch_candidate_exposures", _counts)
        monkeypatch.setattr(exposure, "recent_user_exposures", _recent)

    def test_null_stable_uses_legacy_comparison_and_reads_exposures(self, monkeypatch):
        from app.api.admin.recommendation_strategies import simulate_strategy_draft
        from app.schemas.recommendation import RecommendationSimulationRequest

        candidates = [_job_candidate(1, "o1"), _job_candidate(2, "o2"), _job_candidate(3, "o3")]
        calls: dict = {}
        self._patch_search(monkeypatch, candidates)
        self._patch_exposures(monkeypatch, calls)
        draft = self._draft()
        release = SimpleNamespace(direction="search_job", stable_version_id=None)
        db = _simulation_db(draft, release)
        req = RecommendationSimulationRequest(
            direction="search_job", user_id="tester", criteria={"city": "深圳"}, draft_version_id=11,
        )
        payload = simulate_strategy_draft(11, req, db=db, _=SimpleNamespace(username="admin"))
        data = payload["data"]
        assert data["current_basis"] == "legacy"
        assert len(data["current"]) == 3
        assert data["llm_invoked"] is False
        assert data["simulation_mode"] == "deterministic"
        assert data["call_site"] == "recommendation_simulation"
        assert data["exposure_available"] is True
        # 真实读取曝光与重复曝光，且用被模拟用户的 ID
        assert calls["counts"] == ["1", "2", "3"]
        assert calls["recent"][0] == "tester"
        assert data["rank_changes"]
        assert set(data["candidate_summaries"]) == {"1", "2", "3"}

    def test_simulation_writes_no_fact_rows(self, monkeypatch):
        from app.api.admin.recommendation_strategies import simulate_strategy_draft
        from app.schemas.recommendation import RecommendationSimulationRequest

        candidates = [_job_candidate(1, "o1")]
        self._patch_search(monkeypatch, candidates)
        self._patch_exposures(monkeypatch, {})
        draft = self._draft()
        db = _simulation_db(draft, SimpleNamespace(direction="search_job", stable_version_id=None))
        req = RecommendationSimulationRequest(
            direction="search_job", criteria={}, draft_version_id=11,
        )
        simulate_strategy_draft(11, req, db=db, _=SimpleNamespace(username="admin"))
        # §8.3：不写快照 / 曝光 / 对话日志 —— 唯一写入是 §7.1 的 last_simulated_digest
        db.add.assert_not_called()
        assert draft.last_simulated_digest == "digest-draft"

    def test_stable_version_is_used_when_present(self, monkeypatch):
        from app.api.admin.recommendation_strategies import simulate_strategy_draft
        from app.schemas.recommendation import (
            RecommendationSimulationRequest,
            RecommendationStrategyParameters,
        )

        candidates = [_job_candidate(1, "o1"), _job_candidate(2, "o2")]
        self._patch_search(monkeypatch, candidates)
        self._patch_exposures(monkeypatch, {})
        draft = self._draft()
        stable = SimpleNamespace(
            id=7, direction="search_job", status="published",
            parameters=RecommendationStrategyParameters.from_template(
                "match_first",
            ).model_dump(mode="json"),
        )
        release = SimpleNamespace(direction="search_job", stable_version_id=7)
        db = _simulation_db(draft, release, stable=stable)
        req = RecommendationSimulationRequest(
            direction="search_job", criteria={}, draft_version_id=11,
        )
        data = simulate_strategy_draft(11, req, db=db, _=SimpleNamespace(username="admin"))["data"]
        assert data["current_basis"] == "stable"
        assert all(item["score_detail"] is not None for item in data["current"])

    def test_exposure_failure_degrades_to_neutral(self, monkeypatch):
        from app.api.admin.recommendation_strategies import simulate_strategy_draft
        from app.schemas.recommendation import RecommendationSimulationRequest
        from app.services import recommendation_exposure_service as exposure

        candidates = [_job_candidate(1, "o1")]
        self._patch_search(monkeypatch, candidates)

        def _boom(*args, **kwargs):
            raise RuntimeError("exposure store down")

        monkeypatch.setattr(exposure, "batch_candidate_exposures", _boom)
        draft = self._draft()
        db = _simulation_db(draft, SimpleNamespace(direction="search_job", stable_version_id=None))
        req = RecommendationSimulationRequest(
            direction="search_job", criteria={}, draft_version_id=11,
        )
        data = simulate_strategy_draft(11, req, db=db, _=SimpleNamespace(username="admin"))["data"]
        assert data["exposure_available"] is False

    def test_direction_mismatch_is_rejected(self, monkeypatch):
        from app.api.admin.recommendation_strategies import simulate_strategy_draft
        from app.schemas.recommendation import RecommendationSimulationRequest

        draft = self._draft()
        db = _simulation_db(draft, None)
        req = RecommendationSimulationRequest(
            direction="search_worker", criteria={}, draft_version_id=11,
        )
        with pytest.raises(BusinessException) as excinfo:
            simulate_strategy_draft(11, req, db=db, _=SimpleNamespace(username="admin"))
        assert excinfo.value.code == 40905


class TestReadOnlyGetEndpoints:
    def test_viewer_get_does_not_write(self):
        from app.api.admin.recommendation_strategies import get_strategy

        db = MagicMock()
        db.get.return_value = None
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        data = get_strategy("search_job", db=db, current=SimpleNamespace(
            username="v", role="viewer",
        ))["data"]
        db.commit.assert_not_called()
        db.add.assert_not_called()
        assert data["release"]["execution_mode"] == "off"
        assert data["release"]["initialized"] is False

    def test_operator_get_bootstraps_once(self, monkeypatch):
        from app.api.admin import recommendation_strategies as api

        db = MagicMock()
        db.get.return_value = None
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        monkeypatch.setattr(api, "ensure_initial_release", lambda db, updated_by="": True)
        api.get_strategy("search_job", db=db, current=SimpleNamespace(
            username="op", role="operator",
        ))
        db.commit.assert_called_once()


class TestSimulationSummary:
    def test_summary_excludes_contact_details(self):
        from app.api.admin.recommendation_strategies import _candidate_summary

        summary = _candidate_summary(
            {
                "id": 1, "city": "深圳", "phone": "13800000000",
                "contact_person": "王", "description": "长文本", "owner_userid": "o1",
            },
            "search_job",
        )
        assert summary["city"] == "深圳"
        assert "phone" not in summary
        assert "contact_person" not in summary
        assert "description" not in summary
