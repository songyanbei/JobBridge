"""阶段二 dialogue_applier.apply_decision 单元测试。

每种 state_transition 至少一条；pending_interruption 注入与消费各一条。
"""
from __future__ import annotations

from unittest.mock import MagicMock

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
        attachment_target_type="job",
        attachment_target_id=91,
        awaiting_fields=["salary_floor_monthly"],
        awaiting_frame="job_search",
        awaiting_expires_at="2099-01-01T00:00:00+00:00",
    )
    apply_decision(_decision(state_transition="reset_search"), s)
    assert s.search_criteria == {}
    assert s.awaiting_fields == []
    assert s.candidate_snapshot is None
    assert s.shown_items == []
    assert s.attachment_target_type is None
    assert s.attachment_target_id is None


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


def test_clear_pending_upload_releases_media_and_replacement_context(monkeypatch):
    mark_delete_pending = MagicMock()
    monkeypatch.setattr(
        "app.services.job_media_service.mark_delete_pending",
        mark_delete_pending,
    )
    db = MagicMock()
    s = _session(
        active_flow="upload_collecting",
        pending_upload={"city": "苏州"},
        pending_upload_intent="upload_job",
        pending_upload_mode="replace",
        pending_target_id=42,
        pending_target_version=7,
        pending_operation_id="op-cancel",
        pending_upload_media_ids=[11, 12],
    )

    apply_decision(
        _decision(state_transition="clear_pending_upload"),
        s,
        db=db,
    )

    mark_delete_pending.assert_called_once_with(db, [11, 12])
    db.flush.assert_called_once_with()
    assert s.pending_upload == {}
    assert s.pending_upload_intent is None
    assert s.pending_upload_mode == "create"
    assert s.pending_target_id is None
    assert s.pending_target_version is None
    assert s.pending_operation_id is None
    assert s.pending_upload_media_ids == []
    assert s.active_flow == "idle"


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


# ---------------------------------------------------------------------------
# Phase 5.2：放宽确认状态机
# ---------------------------------------------------------------------------


def test_clear_pending_relaxation_clears_session_field():
    s = _session(pending_relaxation={
        "frame": "job_search",
        "step": "relax_salary_10pct",
        "original_criteria": {"city": ["北京市"]},
    })
    result = apply_decision(_decision(state_transition="clear_pending_relaxation"), s)
    assert s.pending_relaxation is None
    assert result.transition_executed == "clear_pending_relaxation"


def test_apply_relaxation_is_no_op_in_applier(caplog):
    """phased-plan §5.2.4 验收 #5：apply_relaxation 不在 applier 物化（拿不到 db）；
    显式 no-op + 打 dialogue_applier_relaxation_passthrough 日志，**不**走 unknown
    兜底告警。"""
    import logging
    s = _session(pending_relaxation={
        "frame": "job_search", "step": "relax_salary_10pct",
        "original_criteria": {},
    })
    with caplog.at_level(logging.INFO):
        result = apply_decision(_decision(state_transition="apply_relaxation"), s)
    # session.pending_relaxation **未**被清（apply_relaxation 不归 applier 管）
    assert s.pending_relaxation is not None
    # 无 unknown_state_transition warning
    assert not any(
        "unknown state_transition" in r.message
        for r in caplog.records
    )
    # 有 passthrough 日志
    assert any(
        "dialogue_applier_relaxation_passthrough" in r.message
        for r in caplog.records
    )
    assert result.transition_executed == "apply_relaxation"


def test_cancel_relaxation_is_no_op_in_applier(caplog):
    import logging
    s = _session(pending_relaxation={
        "frame": "job_search", "step": "relax_salary_10pct",
        "original_criteria": {},
    })
    with caplog.at_level(logging.INFO):
        result = apply_decision(_decision(state_transition="cancel_relaxation"), s)
    assert s.pending_relaxation is not None  # 不归 applier 管
    assert result.transition_executed == "cancel_relaxation"
    assert any(
        "dialogue_applier_relaxation_passthrough" in r.message
        for r in caplog.records
    )
