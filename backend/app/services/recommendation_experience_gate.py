"""Recommendation experience rollout gate for Phase 5.

This module is intentionally small and deterministic: all user-visible
recommendation copy and soft-preference behavior should flow through the
computed ``RecommendationExperienceFlags`` instead of reading global settings
directly in reducers or formatters.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from app.config import settings
from app.tasks.common import log_event


Direction = Literal["search_job", "search_worker"]


@dataclass(frozen=True)
class RecommendationExperienceFlags:
    show_match_reasons: bool = False
    build_shadow_reasons: bool = False
    soft_preference_ranking: bool = False
    soft_preference_reasons: bool = False
    soft_preference_notice: bool = False
    rollout_bucket: int | None = None

    @classmethod
    def disabled(cls) -> "RecommendationExperienceFlags":
        return cls()


def rollout_bucket(external_userid: str | None) -> int | None:
    if not external_userid:
        return None
    digest = hashlib.md5(external_userid.encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def rollout_hit(external_userid: str | None, percentage: int) -> bool:
    if not external_userid or percentage <= 0:
        return False
    if percentage >= 100:
        return True
    bucket = rollout_bucket(external_userid)
    return bucket is not None and bucket < percentage


def userid_hash(external_userid: str | None) -> str:
    if not external_userid:
        return ""
    return hashlib.sha256(str(external_userid).encode("utf-8")).hexdigest()[:12]


def compute_recommendation_experience_flags(
    external_userid: str | None,
    *,
    direction: Direction | None = None,
    mode: str | None = None,
    emit_log: bool = False,
) -> RecommendationExperienceFlags:
    policy = settings.dialogue_policy
    bucket = rollout_bucket(external_userid)

    if not policy.recommendation_experience_enabled:
        flags = RecommendationExperienceFlags.disabled()
    else:
        show_match_reasons = rollout_hit(
            external_userid,
            policy.recommendation_reason_rollout_percentage,
        )
        build_shadow_reasons = (
            policy.recommendation_reason_shadow_enabled
            and (mode == "shadow" or not show_match_reasons)
        )
        soft_preference_ranking = (
            policy.soft_preference_ranking_enabled
            and rollout_hit(
                external_userid,
                policy.soft_preference_ranking_rollout_percentage,
            )
        )
        soft_preference_reasons = (
            soft_preference_ranking
            and rollout_hit(
                external_userid,
                policy.soft_preference_reason_rollout_percentage,
            )
        )
        soft_preference_notice = (
            soft_preference_ranking
            and rollout_hit(
                external_userid,
                policy.soft_preference_notice_rollout_percentage,
            )
        )
        flags = RecommendationExperienceFlags(
            show_match_reasons=show_match_reasons,
            build_shadow_reasons=build_shadow_reasons,
            soft_preference_ranking=soft_preference_ranking,
            soft_preference_reasons=soft_preference_reasons,
            soft_preference_notice=soft_preference_notice,
            rollout_bucket=bucket,
        )

    if emit_log:
        log_event(
            "recommendation_experience_gate",
            external_userid_hash=userid_hash(external_userid),
            mode=mode or "",
            direction=direction or "",
            show_match_reasons=flags.show_match_reasons,
            build_shadow_reasons=flags.build_shadow_reasons,
            soft_preference_ranking=flags.soft_preference_ranking,
            soft_preference_reasons=flags.soft_preference_reasons,
            soft_preference_notice=flags.soft_preference_notice,
            rollout_bucket=flags.rollout_bucket,
        )
    return flags
