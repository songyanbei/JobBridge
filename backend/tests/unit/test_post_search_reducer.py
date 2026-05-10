"""post_search_reducer 单元测试（Phase 5 §5.0）。

5.0 子阶段验收：
- post_search_reduce 默认返回 PostSearchDecision(action="no_action")
- 不写 session（纯函数）
- 不调 LLM / handler
- DTO 序列化兼容（json round-trip）
- SearchOutcome 字段在主搜索 / 0 召回 / 翻完三场景下齐全
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.schemas.search import SearchOutcome, SearchResult
from app.services.dialogue_reducer import DialogueDecision
from app.services.post_search_reducer import (
    PostSearchContext,
    PostSearchDecision,
    post_search_reduce,
)


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _make_session(role: str = "worker") -> SessionState:
    return SessionState(role=role, search_criteria={"city": ["北京市"]})


def _make_parse() -> DialogueParseResult:
    return DialogueParseResult(
        dialogue_act="chitchat",
        frame_hint="none",
        slots_delta={},
        merge_hint={},
        needs_clarification=False,
        confidence=0.9,
    )


def _make_decision() -> DialogueDecision:
    return DialogueDecision(
        dialogue_act="chitchat",
        resolved_frame="none",
        route_intent="chitchat",
    )


def _make_outcome(**overrides) -> SearchOutcome:
    base = dict(
        direction="search_job",
        criteria_used={"city": ["北京市"]},
        initial_count=3,
        final_count=3,
        desired_count=3,
        low_recall_threshold=3,
    )
    base.update(overrides)
    return SearchOutcome(**base)


# ---------------------------------------------------------------------------
# 默认行为：纯函数 + no_action
# ---------------------------------------------------------------------------


class TestPostSearchReduceDefault:
    """5.0 子阶段：reducer 永远返回 no_action，不读 outcome 字段。"""

    def test_default_returns_no_action(self):
        d = post_search_reduce(
            parse_result=_make_parse(),
            decision=_make_decision(),
            session=_make_session(),
            search_outcome=_make_outcome(),
            role="worker",
        )
        assert isinstance(d, PostSearchDecision)
        assert d.action == "no_action"

    def test_no_action_under_zero_recall(self):
        # 即使 0 召回，5.0 也是 no_action（5.2 才接通）
        outcome = _make_outcome(initial_count=0, final_count=0)
        d = post_search_reduce(
            parse_result=_make_parse(),
            decision=_make_decision(),
            session=_make_session(),
            search_outcome=outcome,
            role="worker",
        )
        assert d.action == "no_action"

    def test_paginate_no_more_under_snapshot_exhausted(self):
        # 5.1 起：snapshot_exhausted=True 时输出 paginate_no_more；5.0 阶段是
        # no_action（已被 5.1 升级覆盖）。
        outcome = _make_outcome(snapshot_exhausted=True, initial_count=0, final_count=0)
        d = post_search_reduce(
            parse_result=_make_parse(),
            decision=_make_decision(),
            session=_make_session(),
            search_outcome=outcome,
            role="worker",
        )
        assert d.action == "paginate_no_more"
        # session.search_criteria 含 city → directions 至少有 city 一项
        dims = [item["dimension"] for item in d.suggested_directions]
        assert "city" in dims

    def test_pure_function_no_session_write(self):
        # 入参 session 不应被改动
        s = _make_session()
        snapshot_before = s.model_dump()
        post_search_reduce(
            parse_result=_make_parse(),
            decision=_make_decision(),
            session=s,
            search_outcome=_make_outcome(),
            role="worker",
        )
        assert s.model_dump() == snapshot_before


# ---------------------------------------------------------------------------
# DTO 兼容
# ---------------------------------------------------------------------------


class TestPostSearchDecisionDTO:
    @pytest.mark.parametrize("action", [
        "show_results",
        "show_results_with_soft_pref_notice",
        "auto_relax_and_retry",
        "suggest_relaxation",
        "ask_clarification",
        "paginate_no_more",
        "no_action",
    ])
    def test_accepts_all_actions(self, action):
        d = PostSearchDecision(action=action)
        assert d.action == action

    def test_rejects_invalid_action(self):
        with pytest.raises(ValidationError):
            PostSearchDecision(action="totally_invalid")

    def test_default_no_action(self):
        d = PostSearchDecision()
        assert d.action == "no_action"
        assert d.relax_step is None
        assert d.clarification is None
        assert d.suggested_directions == []
        assert d.soft_pref_notice is None
        assert d.reasoning == ""

    def test_json_round_trip(self):
        d = PostSearchDecision(
            action="paginate_no_more",
            suggested_directions=[
                {"dimension": "city", "hint_text": "换附近城市", "target_field": "city"},
            ],
            reasoning="show_more exhausted",
        )
        recovered = PostSearchDecision.model_validate_json(d.model_dump_json())
        assert recovered == d


# ---------------------------------------------------------------------------
# PostSearchContext 字段齐全
# ---------------------------------------------------------------------------


class TestPostSearchContextFields:
    """phased-plan §5.0.4 验收 #7：5.1 applier 需要的所有字段在 5.0 已就位，
    db / user_ctx / raw_query / role 即使本子阶段不消费也必须能被填齐。
    """

    def _make_ctx(self) -> PostSearchContext:
        return PostSearchContext(
            decision=PostSearchDecision(action="no_action"),
            search_result=SearchResult(reply_text="hi", has_more=False, result_count=0),
            search_outcome=_make_outcome(),
            parse_result=_make_parse(),
            dialogue_decision=_make_decision(),
            session=_make_session(),
            msg=MagicMock(),
            user_ctx=MagicMock(),
            db=MagicMock(),
            raw_query="北京找工作",
            role="worker",
            recursion_depth=0,
        )

    def test_can_construct_with_all_fields(self):
        ctx = self._make_ctx()
        assert ctx.decision.action == "no_action"
        assert ctx.recursion_depth == 0
        assert ctx.raw_query == "北京找工作"
        assert ctx.role == "worker"

    def test_attribute_access_no_error(self):
        # 5.0 验收第 7 条：访问 5 个 5.1 路径核心字段不抛 AttributeError
        ctx = self._make_ctx()
        _ = ctx.decision
        _ = ctx.search_result
        _ = ctx.search_outcome
        _ = ctx.session
        _ = ctx.msg

    def test_recursion_depth_default_zero(self):
        ctx = PostSearchContext(
            decision=PostSearchDecision(),
            search_result=SearchResult(reply_text=""),
            search_outcome=_make_outcome(),
            parse_result=_make_parse(),
            dialogue_decision=_make_decision(),
            session=_make_session(),
            msg=MagicMock(),
            user_ctx=MagicMock(),
            db=MagicMock(),
            raw_query="x",
            role="worker",
        )
        assert ctx.recursion_depth == 0


# ---------------------------------------------------------------------------
# SearchOutcome 字段齐全
# ---------------------------------------------------------------------------


class TestSearchOutcomeFields:
    """phased-plan §5.0.4 验收第 1 条：SearchOutcome 在 0 召回 / 放宽 / 翻完
    三类场景下字段都齐全（不为 None / 不抛 AttributeError）。
    """

    def test_zero_recall(self):
        o = SearchOutcome(
            direction="search_job",
            criteria_used={"city": ["北京市"]},
            initial_count=0,
            final_count=0,
            desired_count=3,
            low_recall_threshold=3,
        )
        assert o.applied_relax_step is None
        assert o.fallback_suggestions == []
        assert o.soft_pref_hits == {}
        assert o.has_more is False
        assert o.snapshot_exhausted is False

    def test_with_relax_step(self):
        o = SearchOutcome(
            direction="search_job",
            criteria_used={"city": ["北京市"], "salary_floor_monthly": 4500},
            initial_count=0,
            final_count=2,
            desired_count=3,
            low_recall_threshold=3,
            applied_relax_step="relax_salary_10pct",
        )
        assert o.applied_relax_step == "relax_salary_10pct"

    def test_snapshot_exhausted(self):
        o = SearchOutcome(
            direction="search_job",
            criteria_used={"city": ["北京市"]},
            initial_count=0,
            final_count=0,
            desired_count=3,
            low_recall_threshold=3,
            snapshot_exhausted=True,
        )
        assert o.snapshot_exhausted is True


# ---------------------------------------------------------------------------
# Phase 5.1：paginate_no_more 决策（三种 criteria 形态）
# ---------------------------------------------------------------------------


class TestPaginateNoMoreDecision:
    """phased-plan §5.1.1 第 2 项：show_more 翻完时根据 criteria 形态产出
    suggested_directions 列表。三种形态各一条。"""

    def _reduce_with_session(self, session: SessionState) -> PostSearchDecision:
        outcome = _make_outcome(
            direction="search_job",
            snapshot_exhausted=True,
            criteria_used=session.search_criteria,
            initial_count=0,
            final_count=0,
        )
        return post_search_reduce(
            parse_result=_make_parse(),
            decision=_make_decision(),
            session=session,
            search_outcome=outcome,
            role="worker",
        )

    def test_only_city_and_category(self):
        s = SessionState(
            role="worker",
            search_criteria={
                "city": ["北京市"],
                "job_category": ["餐饮"],
            },
        )
        d = self._reduce_with_session(s)
        assert d.action == "paginate_no_more"
        dims = [item["dimension"] for item in d.suggested_directions]
        # 仅 city + job_category 形态：建议换附近城市 / 切换工种大类
        assert "city" in dims
        assert "job_category" in dims
        # 没有 salary 不应出现 salary_floor 建议
        assert "salary_floor" not in dims
        # 没有软偏好不应出现 soft_pref
        assert "soft_pref" not in dims

    def test_with_salary_floor(self):
        s = SessionState(
            role="worker",
            search_criteria={
                "city": ["北京市"],
                "job_category": ["餐饮"],
                "salary_floor_monthly": 5000,
            },
        )
        d = self._reduce_with_session(s)
        assert d.action == "paginate_no_more"
        dims = [item["dimension"] for item in d.suggested_directions]
        assert "salary_floor" in dims
        assert "city" in dims

    def test_with_soft_preference(self):
        s = SessionState(
            role="worker",
            search_criteria={
                "city": ["北京市"],
                "job_category": ["餐饮"],
                "salary_floor_monthly": 5000,
                "provide_meal": True,
            },
        )
        d = self._reduce_with_session(s)
        assert d.action == "paginate_no_more"
        dims = [item["dimension"] for item in d.suggested_directions]
        # 有软偏好时 soft_pref 应排在第一位（先建议放弃软偏好）
        assert dims[0] == "soft_pref"

    def test_candidate_search_direction(self):
        # broker / factory 找工人方向，使用 candidate_search frame
        s = SessionState(
            role="broker",
            search_criteria={
                "city": ["苏州市"],
                "job_category": ["普工"],
                "salary_ceiling_monthly": 6000,
            },
        )
        outcome = _make_outcome(
            direction="search_worker",
            snapshot_exhausted=True,
            criteria_used=s.search_criteria,
        )
        d = post_search_reduce(
            parse_result=_make_parse(),
            decision=_make_decision(),
            session=s,
            search_outcome=outcome,
            role="broker",
        )
        assert d.action == "paginate_no_more"
        dims = [item["dimension"] for item in d.suggested_directions]
        # candidate_search 应使用 salary_ceiling 而非 salary_floor
        assert "salary_ceiling" in dims
        assert "salary_floor" not in dims

    def test_empty_criteria_returns_directions_empty(self):
        # criteria 极简（无任何字段）时 directions 为空，applier 走兜底文案
        s = SessionState(role="worker", search_criteria={})
        d = self._reduce_with_session(s)
        assert d.action == "paginate_no_more"
        assert d.suggested_directions == []
