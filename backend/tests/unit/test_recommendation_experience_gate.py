"""Phase 5 recommendation experience gate tests."""
from app.config import DialoguePolicy, settings
from app.services.recommendation_experience_gate import (
    compute_recommendation_experience_flags,
)


def _set_policy(monkeypatch, **updates):
    original = settings.dialogue_policy
    monkeypatch.setattr(
        settings,
        "dialogue_policy",
        original.model_copy(update=updates),
    )


def test_master_switch_disables_all_flags(monkeypatch):
    _set_policy(
        monkeypatch,
        recommendation_experience_enabled=False,
        recommendation_reason_rollout_percentage=100,
        recommendation_reason_shadow_enabled=True,
        soft_preference_ranking_enabled=True,
        soft_preference_ranking_rollout_percentage=100,
        soft_preference_reason_rollout_percentage=100,
        soft_preference_notice_rollout_percentage=100,
    )

    flags = compute_recommendation_experience_flags("u1", direction="search_job")

    assert flags.show_match_reasons is False
    assert flags.build_shadow_reasons is False
    assert flags.soft_preference_ranking is False
    assert flags.soft_preference_reasons is False
    assert flags.soft_preference_notice is False


def test_rollout_percentages_are_clamped():
    policy = DialoguePolicy(
        recommendation_reason_rollout_percentage=200,
        soft_preference_ranking_rollout_percentage=-5,
        soft_preference_reason_rollout_percentage=37,
        soft_preference_notice_rollout_percentage="bad",
    )

    assert policy.recommendation_reason_rollout_percentage == 100
    assert policy.soft_preference_ranking_rollout_percentage == 0
    assert policy.soft_preference_reason_rollout_percentage == 37
    assert policy.soft_preference_notice_rollout_percentage == 0


def test_soft_preference_reasons_and_notice_depend_on_ranking(monkeypatch):
    _set_policy(
        monkeypatch,
        recommendation_experience_enabled=True,
        soft_preference_ranking_enabled=True,
        soft_preference_ranking_rollout_percentage=0,
        soft_preference_reason_rollout_percentage=100,
        soft_preference_notice_rollout_percentage=100,
    )

    flags = compute_recommendation_experience_flags("u1")

    assert flags.soft_preference_ranking is False
    assert flags.soft_preference_reasons is False
    assert flags.soft_preference_notice is False


def test_soft_preference_global_switch_disables_soft_flags(monkeypatch):
    _set_policy(
        monkeypatch,
        recommendation_experience_enabled=True,
        soft_preference_ranking_enabled=False,
        soft_preference_ranking_rollout_percentage=100,
        soft_preference_reason_rollout_percentage=100,
        soft_preference_notice_rollout_percentage=100,
    )

    flags = compute_recommendation_experience_flags("u1")

    assert flags.soft_preference_ranking is False
    assert flags.soft_preference_reasons is False
    assert flags.soft_preference_notice is False
