"""§7 stable rollout and shadow assignment contracts."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.recommendation_strategy_service import (
    select_assignment,
    shadow_candidate_version_id,
)


def _release(**overrides):
    values = {
        "execution_mode": "on",
        "stable_version_id": 11,
        "candidate_version_id": 12,
        "rollout_percentage": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_on_mode_respects_zero_and_full_rollout_boundaries():
    assert select_assignment(
        release=_release(rollout_percentage=0),
        userid="u1",
        direction="search_job",
    ) == ("stable", 11)
    assert select_assignment(
        release=_release(rollout_percentage=100),
        userid="u1",
        direction="search_job",
    ) == ("candidate", 12)


def test_shadow_always_serves_stable_but_only_schedules_a_bucket_hit():
    release = _release(execution_mode="shadow", rollout_percentage=100)

    assert select_assignment(
        release=release,
        userid="u1",
        direction="search_worker",
    ) == ("stable", 11)
    assert shadow_candidate_version_id(
        release=release,
        userid="u1",
        direction="search_worker",
    ) == 12


def test_kill_switch_forces_legacy_and_disables_shadow():
    release = _release(execution_mode="shadow", stable_version_id=None)

    assert select_assignment(
        release=release,
        userid="u1",
        direction="search_job",
        kill_switch=True,
    ) == ("legacy", None)
    assert shadow_candidate_version_id(
        release=release,
        userid="u1",
        direction="search_job",
        kill_switch=True,
    ) is None
