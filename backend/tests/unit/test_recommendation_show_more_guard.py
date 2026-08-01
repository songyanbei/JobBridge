"""§5.4.1 / §7.5 guards on `show_more` for recommendation-v1 snapshots.

Two separate leaks are covered here:

* a v1 snapshot must page at the fixed size of 3 rather than at whatever the
  historical legacy `match.top_n` happens to be;
* once the kill switch is on, a v1 snapshot must stop serving immediately
  instead of quietly paging out an ordering the operator just disabled.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services.search_service import show_more
from app.services.user_service import UserContext
from app.core.time_utils import utc_now


def _fresh_expires():
    from datetime import timedelta

    return (utc_now() + timedelta(minutes=25)).isoformat()


def _session(*, algorithm_version="legacy", assignment="legacy"):
    session = SessionState(role="worker")
    session.candidate_snapshot = CandidateSnapshot(
        candidate_ids=[str(i) for i in range(1, 11)],
        query_digest="abc",
        expires_at=_fresh_expires(),
        algorithm_version=algorithm_version,
        assignment=assignment,
    )
    session.shown_items = []
    return session


def _user_ctx():
    return UserContext(
        external_userid="u1", role="worker", status="active",
        display_name=None, company=None, contact_person=None, phone=None,
        can_search_jobs=True, can_search_workers=False,
        is_first_touch=False, should_welcome=False,
    )


@patch("app.services.search_service._recommendation_kill_switch", return_value=False)
@patch("app.services.search_service._get_config_int", return_value=8)
@patch("app.services.search_service._jobs_to_dicts", return_value=[])
@patch("app.services.search_service._validate_job_ids", return_value=[])
def test_v1_snapshot_pages_at_the_fixed_v1_size(mock_validate, _dicts, mock_config, _kill):
    """Legacy config says 8; a v1 snapshot must still request 3 per page."""
    session = _session(algorithm_version="recommendation-v1", assignment="candidate")
    show_more(session, _user_ctx(), MagicMock())
    first_batch = mock_validate.call_args_list[0].args[0]
    assert len(first_batch) == 3


@patch("app.services.search_service._recommendation_kill_switch", return_value=False)
@patch("app.services.search_service._get_config_int", return_value=8)
@patch("app.services.search_service._jobs_to_dicts", return_value=[])
@patch("app.services.search_service._validate_job_ids", return_value=[])
def test_legacy_snapshot_still_honours_the_generic_config(mock_validate, _dicts, mock_config, _kill):
    session = _session()
    show_more(session, _user_ctx(), MagicMock())
    first_batch = mock_validate.call_args_list[0].args[0]
    assert len(first_batch) == 8


@patch("app.services.search_service._recommendation_kill_switch", return_value=True)
@patch("app.services.search_service._get_config_int", return_value=3)
def test_kill_switch_invalidates_a_v1_snapshot(mock_config, _kill):
    session = _session(algorithm_version="recommendation-v1", assignment="candidate")
    result, _outcome = show_more(session, _user_ctx(), MagicMock())
    assert session.candidate_snapshot is None
    assert session.shown_items == []
    assert "重新搜索" in result.reply_text


@patch("app.services.search_service._recommendation_kill_switch", return_value=True)
@patch("app.services.search_service._get_config_int", return_value=3)
@patch("app.services.search_service._jobs_to_dicts", return_value=[])
@patch("app.services.search_service._validate_job_ids", return_value=[])
def test_kill_switch_leaves_legacy_snapshots_alone(_ids, _dicts, mock_config, _kill):
    """The switch disables the new strategy, not the legacy baseline."""
    session = _session()
    show_more(session, _user_ctx(), MagicMock())
    assert session.candidate_snapshot is not None


def test_unreadable_control_plane_is_treated_as_killed():
    """§7.5 fail-safe: when neither Redis nor the DB answers, force off."""
    from app.services import search_service

    with patch(
        "app.services.recommendation_strategy_service.runtime_kill_switch",
        side_effect=RuntimeError("control plane down"),
    ):
        assert search_service._recommendation_kill_switch(MagicMock()) is True


@pytest.mark.parametrize(
    "algorithm_version,assignment,expected",
    [
        ("legacy", "legacy", False),
        ("recommendation-v1", "legacy", True),
        ("legacy", "candidate", True),
        ("recommendation-v1", "stable", True),
    ],
)
def test_snapshot_v1_detection(algorithm_version, assignment, expected):
    from app.services.search_service import _snapshot_is_v1

    snapshot = CandidateSnapshot(
        candidate_ids=["1"], algorithm_version=algorithm_version, assignment=assignment,
    )
    assert _snapshot_is_v1(snapshot) is expected
    assert _snapshot_is_v1(None) is False


def test_snapshot_scores_use_the_public_score_detail_contract():
    from app.services.recommendation_request_service import (
        snapshot_candidate_scores,
    )
    from app.services.recommendation_scoring_service import ScoredCandidate

    scores = snapshot_candidate_scores([
        ScoredCandidate(
            candidate_id="4",
            owner_userid="owner-4",
            match_score=0.7,
            quality_score=0.8,
            freshness_score=0.6,
            exposure_opportunity=0.5,
            base_score=0.68,
            repeat_factor=0.9,
            repeat_adjusted_score=0.612,
            diversity_penalty=0.03,
            is_exploration=True,
            reason_codes=["exploration"],
        ),
    ])

    detail = scores["4"]["score_detail"]
    assert detail["repeat_adjusted_score"] == 0.612
    assert detail["is_exploration"] is True
    assert "diversity_penalty" not in detail


@patch("app.services.search_service._recommendation_kill_switch", return_value=False)
@patch("app.services.search_service._get_config_int", return_value=3)
@patch("app.services.search_service._validate_job_ids")
@patch("app.services.search_service._jobs_to_dicts")
@patch("app.services.search_service.permission_service.filter_jobs_batch")
@patch("app.services.search_service._build_job_reason_lines_by_id", return_value={})
@patch("app.services.search_service._format_job_results", return_value="ok")
def test_v1_show_more_repairs_previous_score_contract(
    mock_format,
    _reasons,
    mock_filter,
    mock_dicts,
    mock_validate,
    _config,
    _kill,
):
    """A 30-minute old snapshot must survive the score DTO deploy boundary."""
    candidate = {
        "id": 4,
        "owner_userid": "owner-4",
        "city": "苏州",
        "job_category": "电子厂",
        "salary_floor_monthly": 5000,
    }
    mock_validate.return_value = [MagicMock(id=4, owner_userid="owner-4")]
    mock_dicts.return_value = [candidate]
    mock_filter.return_value = [candidate]
    session = _session(algorithm_version="recommendation-v1", assignment="candidate")
    session.candidate_snapshot.candidate_ids = ["1", "2", "3", "4"]
    session.candidate_snapshot.ranking_metadata = {
        "candidate_scores": {
            "4": {
                "final_score": 0.72,
                "is_exploration": True,
                "reason_codes": ["exploration"],
                "score_detail": {
                    "match_score": 0.7,
                    "quality_score": 0.8,
                    "freshness_score": 0.6,
                    "exposure_opportunity": 0.5,
                    "base_score": 0.68,
                    "repeat_factor": 1.0,
                    # Old producer omitted this and leaked this internal key.
                    "diversity_penalty": 0.01,
                },
            },
        },
    }
    session.shown_items = ["1", "2", "3"]

    result, _outcome = show_more(session, _user_ctx(), MagicMock())

    assert result.result_count == 1
    assert result.recommendation_items[0].score_detail.repeat_adjusted_score == 0.72
    assert result.recommendation_items[0].score_detail.is_exploration is True
    assert session.shown_items == ["1", "2", "3", "4"]
    mock_format.assert_called_once()


@patch("app.services.search_service._recommendation_kill_switch", return_value=False)
@patch("app.services.search_service._get_config_int", return_value=3)
@patch("app.services.search_service._validate_job_ids", return_value=[MagicMock(id=4)])
@patch(
    "app.services.search_service._jobs_to_dicts",
    return_value=[{"id": 4, "owner_userid": "owner-4"}],
)
@patch(
    "app.services.search_service.permission_service.filter_jobs_batch",
    return_value=[{"id": 4, "owner_userid": "owner-4"}],
)
@patch(
    "app.services.search_service._build_job_reason_lines_by_id",
    return_value={},
)
@patch(
    "app.services.search_service._format_job_results",
    side_effect=RuntimeError("format failed"),
)
def test_show_more_failure_does_not_advance_session(
    _format,
    _reasons,
    _filter,
    _dicts,
    _validate,
    _config,
    _kill,
):
    session = _session()
    session.candidate_snapshot.candidate_ids = ["1", "2", "3", "4"]
    session.shown_items = ["1", "2", "3"]

    with pytest.raises(RuntimeError, match="format failed"):
        show_more(session, _user_ctx(), MagicMock())

    assert session.shown_items == ["1", "2", "3"]
