"""P1 acceptance tests for the unified recommendation search gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.exceptions import BusinessException
from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services import account_service, message_router, search_service
from app.services.search_permission import (
    PermissionDecision,
    ResolvedSearchDirection,
    check_search_permission,
    denied_search_response,
)
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage


def _user(
    role: str,
    *,
    can_search_jobs: bool,
    can_search_workers: bool,
) -> UserContext:
    return UserContext(
        external_userid="safe-user-id",
        role=role,
        status="active",
        display_name="测试姓名",
        company="敏感公司",
        contact_person="敏感联系人",
        phone="13800000000",
        can_search_jobs=can_search_jobs,
        can_search_workers=can_search_workers,
        is_first_touch=False,
        should_welcome=False,
    )


@pytest.mark.parametrize(
    ("role", "direction", "jobs", "workers", "allowed", "reason"),
    [
        ("worker", "search_job", True, False, True, "allowed"),
        ("worker", "search_worker", True, True, False, "role_direction_forbidden"),
        ("factory", "search_worker", False, True, True, "allowed"),
        ("factory", "search_job", True, True, False, "role_direction_forbidden"),
        ("broker", "search_job", False, True, False, "account_search_disabled"),
        ("broker", "search_worker", True, False, False, "account_search_disabled"),
        ("broker", "search_job", True, True, True, "allowed"),
        ("unknown", "search_job", True, True, False, "unknown_role"),
    ],
)
def test_role_gate_and_account_switch_must_both_allow(
    role, direction, jobs, workers, allowed, reason,
) -> None:
    decision = check_search_permission(
        _user(role, can_search_jobs=jobs, can_search_workers=workers),
        direction,
        entrypoint="test.matrix",
        request_id="request-1",
    )
    assert decision.allowed is allowed
    assert decision.reason_code == reason


def test_permission_log_contains_only_allowlisted_facts(caplog) -> None:
    caplog.set_level("INFO", logger="app.services.search_permission")
    user = _user("broker", can_search_jobs=False, can_search_workers=True)
    check_search_permission(
        user, "search_job", entrypoint="router", request_id="message-1",
    )
    text = caplog.text
    assert "role=broker" in text
    assert "reason_code=account_search_disabled" in text
    assert "13800000000" not in text
    assert "敏感联系人" not in text
    assert "敏感公司" not in text
    assert "safe-user-id" not in text


def test_denied_response_has_stable_empty_tuple_contract() -> None:
    decision = check_search_permission(
        _user("factory", can_search_jobs=True, can_search_workers=True),
        "search_job",
    )
    result, outcome = denied_search_response(decision)
    assert result.result_count == 0
    assert result.has_more is False
    assert outcome.direction == "search_job"
    assert outcome.has_more is False
    assert outcome.shown_count == 0


def test_public_entry_rejects_forged_allowed_decision() -> None:
    user = _user("factory", can_search_jobs=True, can_search_workers=True)
    forged = PermissionDecision(
        allowed=True,
        role="factory",
        direction="search_job",
        role_allowed=True,
        account_switch="can_search_jobs",
        account_allowed=True,
        reason_code="allowed",
        entrypoint="forged",
    )
    result, outcome = search_service.search_jobs(
        {}, "", SessionState(role="factory"), user, MagicMock(),
        permission_decision=forged,
    )
    assert result.result_count == 0
    assert outcome.direction == "search_job"
    assert "不支持搜索岗位" in result.reply_text


def test_factory_explicit_job_intent_returns_controlled_unsupported_marker() -> None:
    user = _user("factory", can_search_jobs=True, can_search_workers=True)
    direction = message_router._resolve_search_direction(
        "search_job", user, SessionState(role="factory"),
    )
    assert isinstance(direction, ResolvedSearchDirection)
    assert direction == "search_job"
    assert direction.supported is False
    assert direction.reason_code == "role_direction_forbidden"


def test_run_search_rejects_before_defaults_or_search_and_preserves_snapshot() -> None:
    user = _user("broker", can_search_jobs=False, can_search_workers=True)
    snapshot = MagicMock(name="existing_snapshot")
    session = SessionState(role="broker", broker_direction="search_job")
    session.candidate_snapshot = snapshot
    session.shown_items = ["10"]

    with (
        patch.object(message_router, "_apply_default_criteria") as defaults,
        patch.object(search_service, "search_jobs") as actual_search,
    ):
        result, outcome = message_router._run_search(
            "search_job", {"city": ["苏州"]}, "苏州岗位", user, session,
            MagicMock(), user_msg_id="message-2",
        )

    defaults.assert_not_called()
    actual_search.assert_not_called()
    assert result.result_count == 0
    assert outcome.direction == "search_job"
    assert session.candidate_snapshot is snapshot
    assert session.shown_items == ["10"]


def test_show_more_rechecks_current_account_switch_before_paging() -> None:
    user = _user("broker", can_search_jobs=False, can_search_workers=True)
    session = SessionState(role="broker", broker_direction="search_job")
    msg = WeComMessage(msg_id="message-3", from_user="safe-user-id")
    with patch.object(search_service, "show_more") as actual_show_more:
        replies = message_router._handle_show_more(msg, user, session, MagicMock())
    actual_show_more.assert_not_called()
    assert "未开通岗位搜索权限" in replies[0].content


def test_show_more_authorizes_the_snapshot_direction_not_mutable_role_hint() -> None:
    user = _user("factory", can_search_jobs=True, can_search_workers=True)
    session = SessionState(
        role="factory",
        candidate_snapshot=CandidateSnapshot(
            candidate_ids=["1"], direction="search_job",
        ),
    )
    msg = WeComMessage(msg_id="message-snapshot", from_user="safe-user-id")
    with patch.object(search_service, "show_more") as actual_show_more:
        replies = message_router._handle_show_more(msg, user, session, MagicMock())
    actual_show_more.assert_not_called()
    assert "厂家账号不支持搜索岗位" in replies[0].content


def test_confirmed_relaxation_rechecks_permission_and_clears_pending() -> None:
    from app.services.dialogue_reducer import DialogueDecision

    user = _user("broker", can_search_jobs=False, can_search_workers=True)
    session = SessionState(
        role="broker",
        pending_relaxation={
            "direction": "search_job",
            "step": "relax_salary_10pct",
            "original_criteria": {"city": ["苏州"]},
            "raw_query": "苏州岗位",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
        },
    )
    decision = DialogueDecision(
        dialogue_act="respond_relaxation_offer",
        resolved_frame="none",
        route_intent="follow_up",
        state_transition="apply_relaxation",
    )
    msg = WeComMessage(msg_id="message-4", from_user="safe-user-id", content="同意")
    with patch.object(search_service, "execute_relaxed_search") as actual_search:
        replies = message_router._route_v2_relaxation_response(
            decision, msg, user, session, MagicMock(),
        )
    actual_search.assert_not_called()
    assert session.pending_relaxation is None
    assert "未开通岗位搜索权限" in replies[0].content


@pytest.mark.parametrize("entrypoint", ["jobs", "workers", "more", "relaxed"])
def test_public_search_entries_defensively_reject_without_database_or_state_mutation(
    entrypoint,
) -> None:
    db = MagicMock()
    session = SessionState(role="factory")
    session.shown_items = ["existing"]

    if entrypoint == "jobs":
        user = _user("factory", can_search_jobs=True, can_search_workers=True)
        result, outcome = search_service.search_jobs({}, "", session, user, db)
    elif entrypoint == "workers":
        user = _user("worker", can_search_jobs=True, can_search_workers=True)
        result, outcome = search_service.search_workers({}, "", session, user, db)
    elif entrypoint == "more":
        user = _user("factory", can_search_jobs=False, can_search_workers=False)
        result, outcome = search_service.show_more(session, user, db)
    else:
        user = _user("factory", can_search_jobs=True, can_search_workers=True)
        result, outcome = search_service.execute_relaxed_search(
            {}, "relax_city", direction="search_job", raw_query="",
            session=session, user_ctx=user, db=db,
        )

    assert result.result_count == 0
    assert outcome.has_more is False
    assert session.shown_items == ["existing"]
    db.query.assert_not_called()


def test_factory_pre_registration_rejects_job_search_enablement() -> None:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(BusinessException, match="厂家账号不能开通岗位搜索权限"):
        account_service.pre_register(
            db, "factory", {"can_search_jobs": True}, "admin",
        )


def test_factory_edit_rejects_true_and_repairs_legacy_value() -> None:
    factory = SimpleNamespace(
        external_userid="factory-1",
        role="factory",
        display_name="厂家",
        company="工厂",
        contact_person=None,
        phone=None,
        can_search_jobs=1,
        can_search_workers=1,
    )
    db = MagicMock()
    with patch.object(account_service, "get_user", return_value=factory):
        with pytest.raises(BusinessException, match="厂家账号不能开通岗位搜索权限"):
            account_service.update_user(
                db, "factory-1", {"can_search_jobs": True}, "admin",
            )

        account_service.update_user(
            db, "factory-1", {"display_name": "新厂家"}, "admin",
        )
    assert factory.can_search_jobs == 0
