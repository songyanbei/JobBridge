"""Recommendation snapshot mutations commit only with a complete reply."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.time_utils import utc_now
from app.llm.base import RerankResult
from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services.search_service import execute_relaxed_search, search_jobs
from app.services.user_service import UserContext


def _session() -> SessionState:
    from datetime import timedelta

    return SessionState(
        role="worker",
        candidate_snapshot=CandidateSnapshot(
            candidate_ids=["old-1", "old-2"],
            query_digest="old",
            expires_at=(utc_now() + timedelta(minutes=20)).isoformat(),
            direction="search_job",
        ),
        shown_items=["old-1"],
    )


def _user() -> UserContext:
    return UserContext(
        external_userid="worker-1",
        role="worker",
        status="active",
        display_name=None,
        company=None,
        contact_person=None,
        phone=None,
        can_search_jobs=True,
        can_search_workers=False,
        is_first_touch=False,
        should_welcome=False,
    )


def _candidates():
    return [MagicMock(id=index) for index in (1, 2, 3)]


def _candidate_dicts():
    return [
        {
            "id": index,
            "owner_userid": f"owner-{index}",
            "city": "苏州",
            "job_category": "电子厂",
            "salary_floor_monthly": 5000,
        }
        for index in (1, 2, 3)
    ]


def _assert_old_snapshot_unchanged(session: SessionState) -> None:
    assert session.candidate_snapshot is not None
    assert session.candidate_snapshot.candidate_ids == ["old-1", "old-2"]
    assert session.candidate_snapshot.query_digest == "old"
    assert session.shown_items == ["old-1"]


@patch(
    "app.services.recommendation_assignment_service.choose_assignment",
    side_effect=RuntimeError("control unavailable"),
)
@patch("app.services.search_service._is_phase5_policy_enabled_for_user", return_value=False)
@patch("app.services.search_service._get_config_int", return_value=3)
@patch("app.services.search_service._query_jobs")
@patch("app.services.search_service._jobs_to_dicts")
@patch("app.services.search_service._rerank_with_logging")
@patch("app.services.search_service.permission_service.filter_jobs_batch")
@patch("app.services.search_service._build_job_reason_lines_by_id", return_value={})
@patch("app.services.search_service._format_job_results", side_effect=RuntimeError("format failed"))
def test_initial_search_failure_keeps_previous_snapshot(
    _format,
    _reasons,
    mock_filter,
    mock_rerank,
    mock_dicts,
    mock_query,
    _config,
    _phase5,
    _assignment,
):
    candidates = _candidates()
    candidate_dicts = _candidate_dicts()
    mock_query.return_value = candidates
    mock_dicts.return_value = candidate_dicts
    mock_rerank.return_value = RerankResult(ranked_items=candidate_dicts)
    mock_filter.return_value = candidate_dicts
    session = _session()

    with pytest.raises(RuntimeError, match="format failed"):
        search_jobs(
            {"city": ["苏州"], "job_category": ["电子厂"]},
            "苏州电子厂",
            session,
            _user(),
            MagicMock(),
        )

    _assert_old_snapshot_unchanged(session)


@patch(
    "app.services.recommendation_assignment_service.choose_assignment",
    side_effect=RuntimeError("control unavailable"),
)
@patch("app.services.search_service._get_config_int", return_value=3)
@patch("app.services.search_service._query_jobs")
@patch("app.services.search_service._jobs_to_dicts")
@patch("app.services.search_service._rerank_with_logging")
@patch("app.services.search_service.permission_service.filter_jobs_batch")
@patch("app.services.search_service._build_job_reason_lines_by_id", return_value={})
@patch("app.services.search_service._format_job_results", side_effect=RuntimeError("format failed"))
def test_relaxed_search_failure_keeps_previous_snapshot(
    _format,
    _reasons,
    mock_filter,
    mock_rerank,
    mock_dicts,
    mock_query,
    _config,
    _assignment,
):
    candidates = _candidates()
    candidate_dicts = _candidate_dicts()
    mock_query.return_value = candidates
    mock_dicts.return_value = candidate_dicts
    mock_rerank.return_value = RerankResult(ranked_items=candidate_dicts)
    mock_filter.return_value = candidate_dicts
    session = _session()

    with pytest.raises(RuntimeError, match="format failed"):
        execute_relaxed_search(
            {
                "city": ["苏州"],
                "job_category": ["电子厂"],
                "salary_floor_monthly": 5500,
            },
            "relax_salary_10pct",
            direction="search_job",
            raw_query="苏州电子厂工资5500",
            session=session,
            user_ctx=_user(),
            db=MagicMock(),
        )

    _assert_old_snapshot_unchanged(session)
