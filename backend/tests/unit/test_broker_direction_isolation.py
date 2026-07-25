import pytest

from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.services.dialogue_applier import apply_decision
from app.services.dialogue_compat import decision_to_intent_result
from app.services.dialogue_reducer import reduce


def _parse(frame: str, city: str) -> DialogueParseResult:
    return DialogueParseResult(
        dialogue_act="start_search",
        frame_hint=frame,
        slots_delta={"city": [city], "job_category": ["技工"]},
        merge_hint={},
        needs_clarification=False,
        confidence=0.95,
    )


def _session(direction: str, city: str) -> SessionState:
    return SessionState(
        role="broker",
        active_flow="search_active",
        broker_direction=direction,
        search_criteria={"city": [city], "job_category": ["技工"]},
        candidate_snapshot={"direction": direction, "ids": [1]},
        shown_items=["1"],
        last_criteria={"city": [city]},
        pending_relaxation={"direction": direction},
        awaiting_fields=["salary_floor_monthly"],
        awaiting_frame=(
            "candidate_search" if direction == "search_worker" else "job_search"
        ),
    )


def test_broker_candidate_to_job_search_is_fresh_and_clears_directional_state():
    session = _session("search_worker", "苏州市")
    decision = reduce(_parse("job_search", "杭州市"), session, "broker")

    assert decision.clarification is None
    assert decision.route_intent == "search_job"
    assert decision.final_search_criteria["city"] == ["杭州市"]
    assert decision_to_intent_result(decision, session).intent == "search_job"

    apply_decision(decision, session)
    assert session.broker_direction == "search_job"
    assert session.search_criteria["city"] == ["杭州市"]
    assert session.candidate_snapshot is None
    assert session.shown_items == []
    assert session.last_criteria == {}
    assert session.pending_relaxation is None
    assert session.awaiting_fields == []


def test_broker_job_to_candidate_search_is_fresh():
    session = _session("search_job", "杭州市")
    decision = reduce(_parse("candidate_search", "苏州市"), session, "broker")

    assert decision.clarification is None
    assert decision.route_intent == "search_worker"
    assert decision.final_search_criteria["city"] == ["苏州市"]
    assert decision_to_intent_result(decision, session).intent == "search_worker"


def test_broker_explicit_object_switch_survives_model_modify_search_label():
    session = _session("search_worker", "苏州市")
    parse = _parse("candidate_search", "杭州市").model_copy(
        update={"dialogue_act": "modify_search"},
    )

    decision = reduce(
        parse,
        session,
        "broker",
        raw_text="给这位师傅找个杭州焊工岗位",
    )

    assert decision.resolved_frame == "job_search"
    assert decision.route_intent == "search_job"
    assert decision.final_search_criteria["city"] == ["杭州市"]
    assert decision_to_intent_result(decision, session).intent == "search_job"
    apply_decision(decision, session)
    assert session.broker_direction == "search_job"


@pytest.mark.parametrize(
    "text",
    [
        "有个工人想去苏州做电工",
        "我这边有位师傅希望到杭州做焊工",
        "手上有一名求职者打算在无锡找工作",
        "这边来了个工人准备去常州做普工",
        "帮这位工人找苏州电工岗位",
        "替一个求职者找份杭州的活",
        "给那位师傅看看上海的工作",
        "工人想找北京的保安岗位",
    ],
)
def test_broker_explicit_job_beneficiary_overrides_wrong_model_frame(text):
    parse = _parse("candidate_search", "苏州市").model_copy(
        update={"slots_delta": {
            "city": ["苏州市"],
            "job_category": ["技工"],
            "salary_ceiling_monthly": 6000,
        }},
    )

    decision = reduce(parse, _session("search_worker", "杭州市"), "broker", raw_text=text)

    assert decision.route_intent == "search_job"
    assert decision.resolved_frame == "job_search"
    assert decision.accepted_slots_delta["salary_floor_monthly"] == 6000
    assert "salary_ceiling_monthly" not in decision.accepted_slots_delta


@pytest.mark.parametrize(
    "text",
    [
        "帮企业找一个苏州电工工人",
        "替公司招聘一位焊工师傅",
        "给厂家推荐杭州的工人",
        "有公司需要无锡叉车工师傅",
        "工厂想找一名普工师傅",
        "招聘方缺一个候选人",
        "找一个愿意去上海的工人",
        "物色两位北京保安人选",
    ],
)
def test_broker_explicit_recruiter_overrides_wrong_model_frame(text):
    parse = _parse("job_search", "苏州市").model_copy(
        update={"slots_delta": {
            "city": ["苏州市"],
            "job_category": ["技工"],
            "salary_floor_monthly": 6000,
        }},
    )

    decision = reduce(parse, _session("search_job", "杭州市"), "broker", raw_text=text)

    assert decision.route_intent == "search_worker"
    assert decision.resolved_frame == "candidate_search"
    assert decision.accepted_slots_delta["salary_ceiling_monthly"] == 6000
    assert "salary_floor_monthly" not in decision.accepted_slots_delta


def test_broker_ambiguous_trade_and_city_keeps_model_frame():
    parse = _parse("candidate_search", "苏州市")

    decision = reduce(
        parse,
        _session("search_worker", "杭州市"),
        "broker",
        raw_text="看看苏州电工",
    )

    assert decision.resolved_frame == "candidate_search"
    assert decision.route_intent != "search_job"


@pytest.mark.parametrize(
    ("role", "wrong_frame", "expected_frame", "expected_intent"),
    [
        ("worker", "candidate_search", "job_search", "search_job"),
        ("factory", "job_search", "candidate_search", "search_worker"),
    ],
)
def test_single_direction_role_search_frame_is_backend_authoritative(
    role, wrong_frame, expected_frame, expected_intent,
):
    salary_key = (
        "salary_ceiling_monthly"
        if wrong_frame == "candidate_search"
        else "salary_floor_monthly"
    )
    parse = DialogueParseResult(
        dialogue_act="start_search",
        frame_hint=wrong_frame,
        slots_delta={
            "city": ["常州市"],
            "job_category": ["餐饮"],
            salary_key: 6000,
        },
        confidence=0.95,
    )
    session = SessionState(role=role)

    decision = reduce(
        parse,
        session,
        role,
        raw_text="有没有愿意去常州的厨师",
    )

    assert decision.resolved_frame == expected_frame
    assert decision.route_intent == expected_intent
    expected_salary = (
        "salary_floor_monthly"
        if role == "worker"
        else "salary_ceiling_monthly"
    )
    assert decision.accepted_slots_delta[expected_salary] == 6000
