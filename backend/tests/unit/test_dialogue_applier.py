"""阶段二 dialogue_applier.apply_decision 单元测试。

每种 state_transition 至少一条；pending_interruption 注入与消费各一条。
"""
from __future__ import annotations

from app.schemas.conversation import SessionState
from app.services.dialogue_applier import apply_decision
from app.services.dialogue_reducer import DialogueDecision


def _decision(**kwargs) -> DialogueDecision:
    base = dict(
        dialogue_act="chitchat",
        resolved_frame="none",
        accepted_slots_delta={},
        resolved_merge_policy={},
        final_search_criteria={},
        missing_slots=[],
        route_intent="chitchat",
        clarification=None,
        state_transition="none",
        pending_interruption=None,
        awaiting_ops=[],
    )
    base.update(kwargs)
    return DialogueDecision(**base)


def _session(**kwargs) -> SessionState:
    base = dict(role="worker", search_criteria={})
    base.update(kwargs)
    return SessionState(**base)


def test_none_transition_no_op():
    s = _session(search_criteria={"city": ["北京市"]})
    apply_decision(_decision(state_transition="none"), s)
    assert s.search_criteria == {"city": ["北京市"]}


def test_clear_awaiting_clears():
    s = _session(
        awaiting_fields=["salary_floor_monthly"],
        awaiting_frame="job_search",
        awaiting_expires_at="2099-01-01T00:00:00+00:00",
    )
    apply_decision(_decision(state_transition="clear_awaiting"), s)
    assert s.awaiting_fields == []
    assert s.awaiting_frame is None
    assert s.awaiting_expires_at is None


def test_reset_search_wipes_criteria_and_awaiting():
    s = _session(
        search_criteria={"city": ["北京市"]},
        awaiting_fields=["salary_floor_monthly"],
        awaiting_frame="job_search",
        awaiting_expires_at="2099-01-01T00:00:00+00:00",
    )
    apply_decision(_decision(state_transition="reset_search"), s)
    assert s.search_criteria == {}
    assert s.awaiting_fields == []
    assert s.candidate_snapshot is None
    assert s.shown_items == []


def test_clear_pending_upload_resets_active_flow():
    s = _session(
        active_flow="upload_collecting",
        pending_upload={"city": "北京市"},
        pending_upload_intent="upload_job",
        awaiting_field="headcount",
    )
    apply_decision(_decision(state_transition="clear_pending_upload"), s)
    assert s.active_flow == "idle"
    assert s.pending_upload == {}
    assert s.pending_upload_intent is None
    assert s.awaiting_field is None


def test_resume_upload_collecting():
    s = _session(
        active_flow="upload_conflict",
        pending_interruption={"intent": "search_job"},
        conflict_followup_rounds=1,
    )
    apply_decision(_decision(state_transition="resume_upload_collecting"), s)
    assert s.active_flow == "upload_collecting"
    assert s.pending_interruption is None
    assert s.conflict_followup_rounds == 0


def test_enter_search_active_writes_criteria():
    s = _session()
    d = _decision(
        state_transition="enter_search_active",
        final_search_criteria={"city": ["北京市"], "job_category": ["餐饮"]},
    )
    apply_decision(d, s)
    assert s.search_criteria == {"city": ["北京市"], "job_category": ["餐饮"]}
    assert s.active_flow == "search_active"


def test_awaiting_ops_consume_removes_field():
    s = _session(
        awaiting_fields=["salary_floor_monthly"],
        awaiting_frame="job_search",
        awaiting_expires_at="2099-01-01T00:00:00+00:00",
    )
    d = _decision(
        state_transition="none",
        awaiting_ops=[{"op": "consume", "fields": ["salary_floor_monthly"]}],
    )
    apply_decision(d, s)
    assert s.awaiting_fields == []
    assert s.awaiting_frame is None  # 队列空 → 一并清


def test_pending_interruption_injected_on_enter_conflict():
    s = _session(active_flow="upload_collecting")
    d = _decision(
        state_transition="enter_upload_conflict",
        pending_interruption={
            "intent": "search_worker",
            "structured_data": {"job_category": ["普工"]},
            "criteria_patch": [],
            "raw_text": "先帮我找个普工",
        },
    )
    apply_decision(d, s)
    assert s.active_flow == "upload_conflict"
    assert s.pending_interruption is not None
    assert s.pending_interruption["intent"] == "search_worker"
