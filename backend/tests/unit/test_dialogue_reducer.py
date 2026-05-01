"""阶段二 dialogue_reducer.reduce 单元测试。

覆盖：
- (active_flow, frame_hint, dialogue_act) 三维主要组合
- 冲突消解（upload_collecting → search_*） ≥ 3
- 置信度兜底 ≥ 3
- awaiting 消费 ≥ 3
- merge policy（list / 标量 / clarify / replace） ≥ 3
- role 权限 / cancel / reset / show_more / chitchat 短路
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.config import settings
from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.services.dialogue_reducer import DialogueDecision, reduce


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _make_session(role="worker", **kwargs) -> SessionState:
    base = dict(
        role=role,
        active_flow="idle",
        search_criteria={},
        awaiting_fields=[],
        awaiting_frame=None,
        pending_upload={},
        pending_upload_intent=None,
    )
    base.update(kwargs)
    return SessionState(**base)


def _make_parse(**kwargs) -> DialogueParseResult:
    base = dict(
        dialogue_act="chitchat",
        frame_hint="none",
        slots_delta={},
        merge_hint={},
        needs_clarification=False,
        confidence=0.9,
        conflict_action=None,
    )
    base.update(kwargs)
    return DialogueParseResult(**base)


# ---------------------------------------------------------------------------
# 短路：cancel / reset / chitchat / show_more
# ---------------------------------------------------------------------------


class TestShortCircuit:
    def test_cancel_with_upload_collecting_clears_pending(self):
        s = _make_session(active_flow="upload_collecting")
        d = reduce(_make_parse(dialogue_act="cancel"), s, "worker")
        assert d.state_transition == "clear_pending_upload"
        assert d.route_intent == "command"

    def test_cancel_with_awaiting_clears_awaiting(self):
        s = _make_session(awaiting_fields=["salary_floor_monthly"])
        d = reduce(_make_parse(dialogue_act="cancel"), s, "worker")
        assert d.state_transition == "clear_awaiting"

    def test_cancel_with_nothing_no_op(self):
        s = _make_session()
        d = reduce(_make_parse(dialogue_act="cancel"), s, "worker")
        assert d.state_transition == "none"

    def test_reset_returns_reset_search_transition(self):
        s = _make_session(search_criteria={"city": ["北京市"]})
        d = reduce(_make_parse(dialogue_act="reset"), s, "worker")
        assert d.state_transition == "reset_search"
        assert d.final_search_criteria == {}

    def test_chitchat_keeps_criteria(self):
        s = _make_session(search_criteria={"city": ["上海市"]})
        d = reduce(_make_parse(dialogue_act="chitchat"), s, "worker")
        assert d.route_intent == "chitchat"
        assert d.final_search_criteria == {"city": ["上海市"]}

    def test_show_more(self):
        s = _make_session()
        d = reduce(_make_parse(dialogue_act="show_more"), s, "worker")
        assert d.route_intent == "show_more"


# ---------------------------------------------------------------------------
# active_flow 冲突
# ---------------------------------------------------------------------------


class TestActiveFlowConflict:
    def test_upload_collecting_to_job_search_enters_conflict(self):
        s = _make_session(
            active_flow="upload_collecting",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
        )
        parse = _make_parse(
            dialogue_act="start_search",
            frame_hint="job_search",
            slots_delta={"city": ["上海市"]},
        )
        d = reduce(parse, s, "broker")
        assert d.state_transition == "enter_upload_conflict"
        assert d.pending_interruption is not None
        assert d.pending_interruption["intent"] == "search_job"

    def test_upload_collecting_to_candidate_search_enters_conflict(self):
        s = _make_session(
            active_flow="upload_collecting",
            pending_upload_intent="upload_job",
        )
        parse = _make_parse(
            dialogue_act="start_search",
            frame_hint="candidate_search",
            slots_delta={"job_category": ["普工"]},
        )
        d = reduce(parse, s, "broker")
        assert d.state_transition == "enter_upload_conflict"

    def test_idle_to_search_does_not_enter_conflict(self):
        s = _make_session(active_flow="idle")
        parse = _make_parse(
            dialogue_act="start_search",
            frame_hint="job_search",
            slots_delta={"city": ["北京市"], "job_category": ["餐饮"]},
        )
        d = reduce(parse, s, "worker")
        assert d.state_transition != "enter_upload_conflict"


# ---------------------------------------------------------------------------
# 置信度兜底
# ---------------------------------------------------------------------------


class TestLowConfidenceOverride:
    def test_low_confidence_with_key_field_forces_clarify(self):
        s = _make_session()
        parse = _make_parse(
            dialogue_act="start_search",
            frame_hint="job_search",
            slots_delta={"city": ["北京市"]},
            confidence=0.4,
        )
        d = reduce(parse, s, "worker")
        assert d.clarification is not None
        assert d.clarification["kind"] == "low_confidence"

    def test_low_confidence_without_key_field_no_force(self):
        # 无 key field：不强制 clarify
        s = _make_session()
        parse = _make_parse(
            dialogue_act="chitchat",
            confidence=0.4,
        )
        d = reduce(parse, s, "worker")
        assert d.clarification is None  # chitchat 已短路，不会进 reducer 主路径

    def test_high_confidence_with_key_field_no_force(self):
        s = _make_session()
        parse = _make_parse(
            dialogue_act="start_search",
            frame_hint="job_search",
            slots_delta={"city": ["北京市"], "job_category": ["餐饮"]},
            confidence=0.9,
        )
        d = reduce(parse, s, "worker")
        assert d.clarification is None


# ---------------------------------------------------------------------------
# Awaiting 消费
# ---------------------------------------------------------------------------


def _future_iso(seconds: int = 600) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=seconds)
    ).isoformat()


def _past_iso() -> str:
    return _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc).isoformat()


class TestAwaitingConsume:
    def test_bare_value_falls_into_salary_when_awaiting(self):
        s = _make_session(
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
            awaiting_expires_at=_future_iso(),
        )
        parse = _make_parse(
            dialogue_act="answer_missing_slot",
            frame_hint="job_search",
            slots_delta={},
        )
        d = reduce(parse, s, "worker", raw_text="2500")
        assert d.accepted_slots_delta.get("salary_floor_monthly") == 2500
        assert any(op.get("op") == "consume" for op in d.awaiting_ops)

    def test_expired_awaiting_does_not_consume(self):
        s = _make_session(
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
            awaiting_expires_at=_past_iso(),
        )
        parse = _make_parse(
            dialogue_act="answer_missing_slot",
            frame_hint="job_search",
            slots_delta={},
        )
        d = reduce(parse, s, "worker", raw_text="2500")
        assert "salary_floor_monthly" not in d.accepted_slots_delta

    def test_cross_frame_isolation(self):
        # awaiting 属于 candidate_search，但当轮 frame=job_search → 不消费
        s = _make_session(
            search_criteria={"city": ["上海市"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="candidate_search",
            awaiting_expires_at=_future_iso(),
        )
        parse = _make_parse(
            dialogue_act="answer_missing_slot",
            frame_hint="job_search",
            slots_delta={},
        )
        d = reduce(parse, s, "broker", raw_text="2500")
        assert "salary_floor_monthly" not in d.accepted_slots_delta


# ---------------------------------------------------------------------------
# merge policy
# ---------------------------------------------------------------------------


class TestMergePolicy:
    def test_replace_hint_takes_precedence(self):
        s = _make_session(search_criteria={"city": ["北京市"]})
        parse = _make_parse(
            dialogue_act="modify_search",
            frame_hint="job_search",
            slots_delta={"city": ["苏州市"]},
            merge_hint={"city": "replace"},
        )
        d = reduce(parse, s, "worker")
        assert d.resolved_merge_policy.get("city") == "replace"
        assert d.final_search_criteria.get("city") == ["苏州市"]

    def test_add_hint_unions_lists(self):
        s = _make_session(search_criteria={"city": ["北京市"]})
        parse = _make_parse(
            dialogue_act="modify_search",
            frame_hint="job_search",
            slots_delta={"city": ["苏州市"]},
            merge_hint={"city": "add"},
        )
        d = reduce(parse, s, "worker")
        assert d.resolved_merge_policy.get("city") == "add"
        assert sorted(d.final_search_criteria.get("city")) == ["北京市", "苏州市"]

    def test_unknown_with_old_value_clarify_policy(self):
        original = settings.ambiguous_city_query_policy
        settings.ambiguous_city_query_policy = "clarify"
        try:
            s = _make_session(search_criteria={"city": ["西安市"]})
            parse = _make_parse(
                dialogue_act="modify_search",
                frame_hint="job_search",
                slots_delta={"city": ["北京市"]},
                merge_hint={"city": "unknown"},
            )
            d = reduce(parse, s, "worker")
            assert d.clarification is not None
            assert d.clarification["kind"] == "city_replace_or_add"
            # clarify 路径下不写 final_search_criteria
            assert d.final_search_criteria.get("city") == ["西安市"]
        finally:
            settings.ambiguous_city_query_policy = original

    def test_unknown_with_old_value_replace_policy(self):
        original = settings.ambiguous_city_query_policy
        settings.ambiguous_city_query_policy = "replace"
        try:
            s = _make_session(search_criteria={"city": ["西安市"]})
            parse = _make_parse(
                dialogue_act="modify_search",
                frame_hint="job_search",
                slots_delta={"city": ["北京市"]},
                merge_hint={"city": "unknown"},
            )
            d = reduce(parse, s, "worker")
            assert d.clarification is None
            assert d.resolved_merge_policy.get("city") == "replace"
            assert d.final_search_criteria.get("city") == ["北京市"]
        finally:
            settings.ambiguous_city_query_policy = original


# ---------------------------------------------------------------------------
# Role 权限
# ---------------------------------------------------------------------------


class TestUnresolvedFrameClarify:
    """adversarial review C5：start_search 但 frame 解析不到 → clarify，不静默 0 命中。"""

    def test_start_search_frame_none_no_criteria_clarifies(self):
        s = _make_session(role="worker")
        parse = _make_parse(
            dialogue_act="start_search",
            frame_hint="none",
            slots_delta={},
            confidence=0.8,
        )
        d = reduce(parse, s, "worker")
        assert d.clarification is not None
        assert d.clarification["kind"] == "low_confidence"
        assert d.route_intent == "chitchat"

    def test_modify_search_frame_none_no_inferable_clarifies(self):
        # broker 有 candidate_search criteria 时，frame_hint=none + modify_search
        # 应能从 broker_direction 推 frame，不触发 clarify
        s = _make_session(
            role="broker",
            search_criteria={"job_category": ["普工"]},
            broker_direction="search_worker",
        )
        parse = _make_parse(
            dialogue_act="modify_search",
            frame_hint="none",
            slots_delta={"city": ["北京市"]},
        )
        d = reduce(parse, s, "broker")
        # broker_direction=search_worker 推出 candidate_search
        assert d.resolved_frame == "candidate_search"
        assert d.clarification is None


class TestRolePermission:
    def test_worker_to_job_upload_denied(self):
        s = _make_session(role="worker")
        parse = _make_parse(
            dialogue_act="start_upload",
            frame_hint="job_upload",
            slots_delta={"job_category": ["餐饮"]},
        )
        d = reduce(parse, s, "worker")
        assert d.clarification is not None
        assert d.clarification["kind"] == "role_no_permission"

    def test_factory_to_job_search_denied(self):
        s = _make_session(role="factory")
        parse = _make_parse(
            dialogue_act="start_search",
            frame_hint="job_search",
            slots_delta={"city": ["北京市"], "job_category": ["餐饮"]},
        )
        d = reduce(parse, s, "factory")
        assert d.clarification is not None
        assert d.clarification["kind"] == "role_no_permission"

    def test_broker_can_both_search(self):
        s = _make_session(role="broker")
        parse = _make_parse(
            dialogue_act="start_search",
            frame_hint="candidate_search",
            slots_delta={"job_category": ["普工"]},
        )
        d = reduce(parse, s, "broker")
        assert d.clarification is None
