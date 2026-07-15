"""Phase 5.4 可见性文案 + 灰度配置单测。

phased-plan §5.4 验收：
- soft_preference_visibility_template 模板（多偏好拼接 / 包吃住合并 / 空兜底）
- 命中阈值 0.5（边界 0.49 / 0.50 / 0.51）
- show_results_with_soft_pref_notice 在 settings.soft_preference_ranking_enabled=True
  + 命中阈值满足时由 reducer 输出
- applier 渲染将 notice 拼到 reply_text 前缀
- _count_soft_pref_hits 统计正确
- phase5_rollout_percentage 配置位 + clamp 0..100
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.dialogue import slot_schema
from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.schemas.search import SearchOutcome, SearchResult
from app.services.dialogue_reducer import DialogueDecision
from app.services.post_search_applier import apply_post_search_decision
from app.services.post_search_reducer import (
    PostSearchContext,
    PostSearchDecision,
    post_search_reduce,
)
from app.services.recommendation_experience_gate import RecommendationExperienceFlags
from app.services.search_service import _count_soft_pref_hits


# ---------------------------------------------------------------------------
# soft_preference_visibility_template
# ---------------------------------------------------------------------------


class TestVisibilityTemplate:
    def test_empty_returns_empty(self):
        assert slot_schema.soft_preference_visibility_template([]) == ""

    def test_single_provide_meal(self):
        text = slot_schema.soft_preference_visibility_template(["provide_meal"])
        assert "包吃" in text
        assert "已优先展示" in text

    def test_meal_and_housing_merge_to_eat_live(self):
        """provide_meal + provide_housing 合并为"包吃住"。"""
        text = slot_schema.soft_preference_visibility_template(
            ["provide_meal", "provide_housing"],
        )
        assert "包吃住" in text
        assert "包吃、包住" not in text  # 不应分开

    def test_meal_only(self):
        text = slot_schema.soft_preference_visibility_template(["provide_meal"])
        assert "包吃" in text
        assert "包住" not in text

    def test_meal_housing_plus_shift(self):
        text = slot_schema.soft_preference_visibility_template(
            ["provide_meal", "provide_housing", "shift_pattern"],
        )
        assert "包吃住" in text
        assert "班次" in text

    def test_unknown_field_skipped(self):
        # 不在表中的字段被忽略
        text = slot_schema.soft_preference_visibility_template(["totally_unknown_field"])
        assert text == ""


# ---------------------------------------------------------------------------
# Reducer：show_results_with_soft_pref_notice 触发判定
# ---------------------------------------------------------------------------


def _make_outcome(
    *,
    final_count: int = 3,
    soft_pref_hits: dict | None = None,
) -> SearchOutcome:
    return SearchOutcome(
        direction="search_job",
        criteria_used={"city": ["北京市"], "job_category": ["餐饮"]},
        initial_count=final_count,
        final_count=final_count,
        desired_count=3,
        low_recall_threshold=3,
        soft_pref_hits=dict(soft_pref_hits or {}),
    )


def _reduce(
    outcome: SearchOutcome,
    experience_flags: RecommendationExperienceFlags | None = None,
) -> PostSearchDecision:
    parse = DialogueParseResult(
        dialogue_act="start_search", frame_hint="job_search",
        slots_delta={}, merge_hint={}, needs_clarification=False,
        confidence=0.9,
    )
    decision = DialogueDecision(
        dialogue_act="start_search", resolved_frame="job_search",
        route_intent="search_job",
    )
    session = SessionState(role="worker", search_criteria={"city": ["北京市"]})
    return post_search_reduce(
        parse_result=parse,
        decision=decision,
        session=session,
        search_outcome=outcome,
        role="worker",
        experience_flags=experience_flags,
    )


class TestSoftPrefNoticeReducerDecision:
    def test_disabled_returns_no_action(self, monkeypatch):
        monkeypatch.setattr(settings, "soft_preference_ranking_enabled", False)
        outcome = _make_outcome(
            final_count=3, soft_pref_hits={"provide_meal": 3},
        )
        d = _reduce(outcome)
        assert d.action == "no_action"

    def test_enabled_above_threshold_triggers(self, monkeypatch):
        monkeypatch.setattr(settings, "soft_preference_ranking_enabled", True)
        # 3/3 = 1.0 ≥ 0.5
        outcome = _make_outcome(
            final_count=3, soft_pref_hits={"provide_meal": 3},
        )
        d = _reduce(
            outcome,
            RecommendationExperienceFlags(soft_preference_notice=True),
        )
        assert d.action == "show_results_with_soft_pref_notice"
        assert d.soft_pref_notice is not None
        assert "包吃" in d.soft_pref_notice

    def test_threshold_boundary_below_50(self, monkeypatch):
        """命中比例 0.49 < 0.5 → 不触发"""
        monkeypatch.setattr(settings, "soft_preference_ranking_enabled", True)
        # 49/100 = 0.49
        outcome = _make_outcome(
            final_count=100, soft_pref_hits={"provide_meal": 49},
        )
        d = _reduce(
            outcome,
            RecommendationExperienceFlags(soft_preference_notice=True),
        )
        assert d.action == "no_action"

    def test_threshold_boundary_exactly_50(self, monkeypatch):
        """命中比例 0.5 ≥ 0.5 → 触发"""
        monkeypatch.setattr(settings, "soft_preference_ranking_enabled", True)
        outcome = _make_outcome(
            final_count=100, soft_pref_hits={"provide_meal": 50},
        )
        d = _reduce(
            outcome,
            RecommendationExperienceFlags(soft_preference_notice=True),
        )
        assert d.action == "show_results_with_soft_pref_notice"

    def test_zero_results_does_not_trigger(self, monkeypatch):
        """final_count=0 不应触发可见性文案。"""
        monkeypatch.setattr(settings, "soft_preference_ranking_enabled", True)
        outcome = _make_outcome(
            final_count=0, soft_pref_hits={},
        )
        # 0 结果会先走 _decide_zero_result（initial_count<threshold）→ no_action 或其他
        d = _reduce(
            outcome,
            RecommendationExperienceFlags(soft_preference_notice=True),
        )
        # 不应是 show_results_with_soft_pref_notice
        assert d.action != "show_results_with_soft_pref_notice"

    def test_empty_hits_does_not_trigger(self, monkeypatch):
        monkeypatch.setattr(settings, "soft_preference_ranking_enabled", True)
        outcome = _make_outcome(final_count=3, soft_pref_hits={})
        d = _reduce(
            outcome,
            RecommendationExperienceFlags(soft_preference_notice=True),
        )
        assert d.action == "no_action"


# ---------------------------------------------------------------------------
# Applier：渲染 notice 拼到 reply_text 前缀
# ---------------------------------------------------------------------------


class TestSoftPrefNoticeApplier:
    def _make_ctx(self, decision: PostSearchDecision) -> PostSearchContext:
        msg = MagicMock()
        msg.from_user = "u-1"
        msg.content = "test"
        msg.msg_id = "m-1"
        user_ctx = MagicMock()
        user_ctx.role = "worker"
        return PostSearchContext(
            decision=decision,
            search_result=SearchResult(reply_text="原 reply 文案", result_count=3),
            search_outcome=_make_outcome(
                final_count=3, soft_pref_hits={"provide_meal": 3},
            ),
            parse_result=DialogueParseResult(
                dialogue_act="start_search", frame_hint="job_search",
                slots_delta={}, merge_hint={}, needs_clarification=False,
                confidence=1.0,
            ),
            dialogue_decision=DialogueDecision(
                dialogue_act="start_search", resolved_frame="job_search",
                route_intent="search_job",
            ),
            session=SessionState(role="worker"),
            msg=msg,
            user_ctx=user_ctx,
            db=MagicMock(),
            raw_query="test",
            role="worker",
            recursion_depth=0,
        )

    def test_renders_notice_as_prefix(self):
        decision = PostSearchDecision(
            action="show_results_with_soft_pref_notice",
            soft_pref_notice="已优先展示符合「包吃」偏好的岗位。",
        )
        ctx = self._make_ctx(decision)
        replies = apply_post_search_decision(ctx)
        assert "已优先展示符合「包吃」" in replies[0].content
        assert "原 reply 文案" in replies[0].content
        # 顺序：notice + \n\n + reply
        assert replies[0].content.startswith("已优先展示")

    def test_empty_notice_returns_only_base(self):
        decision = PostSearchDecision(
            action="show_results_with_soft_pref_notice",
            soft_pref_notice="",
        )
        ctx = self._make_ctx(decision)
        replies = apply_post_search_decision(ctx)
        assert replies[0].content == "原 reply 文案"


# ---------------------------------------------------------------------------
# _count_soft_pref_hits 统计
# ---------------------------------------------------------------------------


class TestCountSoftPrefHits:
    def test_empty_inputs(self):
        assert _count_soft_pref_hits([], {"provide_meal": True}) == {}
        assert _count_soft_pref_hits([{"id": 1}], {}) == {}
        assert _count_soft_pref_hits([{"id": 1}], None) == {}

    def test_single_hit(self):
        candidates = [{"id": 1, "provide_meal": True}]
        assert _count_soft_pref_hits(candidates, {"provide_meal": True}) == {
            "provide_meal": 1,
        }

    def test_multiple_hits(self):
        candidates = [
            {"id": 1, "provide_meal": True},
            {"id": 2, "provide_meal": True},
            {"id": 3, "provide_meal": False},
        ]
        assert _count_soft_pref_hits(candidates, {"provide_meal": True}) == {
            "provide_meal": 2,
        }

    def test_zero_hits_omits_key(self):
        """命中数为 0 的字段不进结果 dict。"""
        candidates = [{"id": 1, "provide_meal": False}]
        assert _count_soft_pref_hits(candidates, {"provide_meal": True}) == {}

    def test_multi_field(self):
        candidates = [
            {"id": 1, "provide_meal": True, "shift_pattern": "日班"},
            {"id": 2, "provide_meal": True, "shift_pattern": "夜班"},
        ]
        assert _count_soft_pref_hits(
            candidates,
            {"provide_meal": True, "shift_pattern": "日班"},
        ) == {"provide_meal": 2, "shift_pattern": 1}


# ---------------------------------------------------------------------------
# phase5_rollout_percentage 配置位
# ---------------------------------------------------------------------------


class TestPhase5RolloutConfig:
    def test_default_zero(self):
        from app.config import Settings
        s = Settings()
        assert s.dialogue_policy.phase5_rollout_percentage == 0

    def test_clamp_above_100(self):
        from app.config import DialoguePolicy
        p = DialoguePolicy(phase5_rollout_percentage=150)
        assert p.phase5_rollout_percentage == 100

    def test_clamp_negative(self):
        from app.config import DialoguePolicy
        p = DialoguePolicy(phase5_rollout_percentage=-5)
        assert p.phase5_rollout_percentage == 0

    def test_valid_value(self):
        from app.config import DialoguePolicy
        p = DialoguePolicy(phase5_rollout_percentage=25)
        assert p.phase5_rollout_percentage == 25
