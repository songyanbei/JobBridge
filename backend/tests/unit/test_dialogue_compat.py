"""阶段二 dialogue_compat.decision_to_intent_result 单元测试。

覆盖 current-state §6.2 三维派生表所有行。
"""
from __future__ import annotations

from app.schemas.conversation import SessionState
from app.services.dialogue_compat import decision_to_intent_result
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


def test_start_search_idle_to_search_job():
    d = _decision(
        dialogue_act="start_search", resolved_frame="job_search",
        accepted_slots_delta={"city": ["北京市"]},
        route_intent="search_job",
    )
    ir = decision_to_intent_result(d, _session())
    assert ir.intent == "search_job"
    assert ir.structured_data == {"city": ["北京市"]}


def test_start_search_with_existing_criteria_to_follow_up():
    s = _session(search_criteria={"city": ["上海市"]})
    d = _decision(
        dialogue_act="start_search", resolved_frame="job_search",
        accepted_slots_delta={"city": ["北京市"]},
        final_search_criteria={"city": ["北京市"]},
        route_intent="follow_up",
    )
    ir = decision_to_intent_result(d, s)
    assert ir.intent == "follow_up"
    assert ir.structured_data == {"city": ["北京市"]}


def test_modify_search_to_follow_up_with_full_snapshot():
    s = _session(search_criteria={"city": ["西安市"]})
    d = _decision(
        dialogue_act="modify_search", resolved_frame="job_search",
        accepted_slots_delta={"salary_floor_monthly": 2500},
        final_search_criteria={
            "city": ["西安市"], "salary_floor_monthly": 2500,
        },
        route_intent="follow_up",
    )
    ir = decision_to_intent_result(d, s)
    assert ir.intent == "follow_up"
    # follow_up 必须用全量快照（避免回到 criteria_patch op 歧义）
    assert ir.structured_data == {"city": ["西安市"], "salary_floor_monthly": 2500}


def test_answer_missing_slot_to_follow_up():
    s = _session(search_criteria={"city": ["西安市"], "job_category": ["餐饮"]})
    d = _decision(
        dialogue_act="answer_missing_slot", resolved_frame="job_search",
        accepted_slots_delta={"salary_floor_monthly": 2500},
        final_search_criteria={
            "city": ["西安市"], "job_category": ["餐饮"], "salary_floor_monthly": 2500,
        },
        route_intent="follow_up",
    )
    ir = decision_to_intent_result(d, s)
    assert ir.intent == "follow_up"


def test_start_upload_job_upload_to_upload_job():
    d = _decision(
        dialogue_act="start_upload", resolved_frame="job_upload",
        accepted_slots_delta={"job_category": ["餐饮"]},
        route_intent="upload_job",
    )
    ir = decision_to_intent_result(d, _session(role="broker"))
    assert ir.intent == "upload_job"


def test_start_upload_resume_upload_to_upload_resume():
    d = _decision(
        dialogue_act="start_upload", resolved_frame="resume_upload",
        accepted_slots_delta={"expected_cities": ["北京市"]},
        route_intent="upload_resume",
    )
    ir = decision_to_intent_result(d, _session())
    assert ir.intent == "upload_resume"


def test_start_search_candidate_search_to_search_worker():
    d = _decision(
        dialogue_act="start_search", resolved_frame="candidate_search",
        accepted_slots_delta={"job_category": ["普工"]},
        route_intent="search_worker",
    )
    ir = decision_to_intent_result(d, _session(role="broker"))
    assert ir.intent == "search_worker"


def test_show_more():
    ir = decision_to_intent_result(
        _decision(dialogue_act="show_more", route_intent="show_more"),
        _session(),
    )
    assert ir.intent == "show_more"


def test_cancel():
    ir = decision_to_intent_result(
        _decision(dialogue_act="cancel", route_intent="command"),
        _session(),
    )
    assert ir.intent == "command"
    assert ir.structured_data == {"command": "cancel_pending"}


def test_reset():
    ir = decision_to_intent_result(
        _decision(dialogue_act="reset", route_intent="command"),
        _session(),
    )
    assert ir.intent == "command"
    assert ir.structured_data == {"command": "reset_search"}


def test_chitchat():
    ir = decision_to_intent_result(
        _decision(dialogue_act="chitchat"),
        _session(),
    )
    assert ir.intent == "chitchat"
