"""conversation_service 单元测试。"""
from unittest.mock import patch

import pytest

from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services.conversation_service import (
    compute_query_digest,
    create_session,
    get_next_candidate_ids,
    increment_follow_up,
    merge_criteria_patch,
    record_history,
    record_shown,
    reset_search,
    save_snapshot,
    set_broker_direction,
)


def _make_session(**kwargs) -> SessionState:
    return SessionState(role=kwargs.pop("role", "worker"), **kwargs)


class TestMergeCriteriaPatch:
    def test_add_to_list(self):
        session = _make_session(search_criteria={"city": ["苏州市"]})
        changed = merge_criteria_patch(session, [
            {"op": "add", "field": "city", "value": ["昆山市"]},
        ])
        assert changed is True
        assert "昆山市" in session.search_criteria["city"]
        assert "苏州市" in session.search_criteria["city"]

    def test_add_dedup(self):
        session = _make_session(search_criteria={"city": ["苏州市"]})
        merge_criteria_patch(session, [
            {"op": "add", "field": "city", "value": ["苏州市"]},
        ])
        assert session.search_criteria["city"].count("苏州市") == 1

    def test_update_replaces(self):
        session = _make_session(search_criteria={"salary_floor_monthly": 5000})
        changed = merge_criteria_patch(session, [
            {"op": "update", "field": "salary_floor_monthly", "value": 6000},
        ])
        assert changed is True
        assert session.search_criteria["salary_floor_monthly"] == 6000

    def test_remove_value_from_list(self):
        session = _make_session(search_criteria={"city": ["苏州市", "昆山市"]})
        changed = merge_criteria_patch(session, [
            {"op": "remove", "field": "city", "value": "昆山市"},
        ])
        assert changed is True
        assert session.search_criteria["city"] == ["苏州市"]

    def test_remove_whole_field(self):
        session = _make_session(search_criteria={"city": ["苏州市"], "salary_floor_monthly": 5000})
        merge_criteria_patch(session, [
            {"op": "remove", "field": "salary_floor_monthly", "value": None},
        ])
        assert "salary_floor_monthly" not in session.search_criteria

    def test_no_change_returns_false(self):
        session = _make_session(search_criteria={"city": ["苏州市"]})
        changed = merge_criteria_patch(session, [
            {"op": "update", "field": "city", "value": ["苏州市"]},
        ])
        assert changed is False

    def test_change_clears_snapshot(self):
        session = _make_session(
            search_criteria={"city": ["苏州市"]},
            candidate_snapshot=CandidateSnapshot(candidate_ids=["1", "2"]),
            shown_items=["1"],
        )
        merge_criteria_patch(session, [
            {"op": "update", "field": "salary_floor_monthly", "value": 5000},
        ])
        assert session.candidate_snapshot is None
        assert session.shown_items == []


class TestComputeQueryDigest:
    def test_stable_ordering(self):
        d1 = compute_query_digest({"b": 2, "a": 1})
        d2 = compute_query_digest({"a": 1, "b": 2})
        assert d1 == d2

    def test_empty_returns_empty(self):
        assert compute_query_digest({}) == ""

    def test_different_values(self):
        d1 = compute_query_digest({"a": 1})
        d2 = compute_query_digest({"a": 2})
        assert d1 != d2


class TestResetSearch:
    def test_clears_search_state(self):
        session = _make_session(
            search_criteria={"city": ["苏州市"]},
            candidate_snapshot=CandidateSnapshot(candidate_ids=["1"]),
            shown_items=["1"],
            follow_up_rounds=2,
            broker_direction="search_job",
        )
        reset_search(session)
        assert session.search_criteria == {}
        assert session.candidate_snapshot is None
        assert session.shown_items == []
        assert session.follow_up_rounds == 0
        assert session.history == []

    def test_preserves_broker_direction(self):
        session = _make_session(role="broker", broker_direction="search_job")
        reset_search(session)
        assert session.broker_direction == "search_job"


class TestRecordHistory:
    def test_appends(self):
        session = _make_session()
        record_history(session, "user", "hello")
        assert len(session.history) == 1
        assert session.history[0] == {"role": "user", "content": "hello"}

    def test_truncates_to_12(self):
        session = _make_session()
        for i in range(15):
            record_history(session, "user" if i % 2 == 0 else "assistant", f"msg{i}")
        assert len(session.history) == 12


class TestSnapshotAndPagination:
    def test_save_and_get_next(self):
        session = _make_session()
        save_snapshot(session, ["1", "2", "3", "4", "5"], "abc123")
        assert session.candidate_snapshot is not None
        assert session.candidate_snapshot.candidate_ids == ["1", "2", "3", "4", "5"]
        assert session.shown_items == []

        ids = get_next_candidate_ids(session, 3)
        assert ids == ["1", "2", "3"]

    def test_get_next_respects_shown(self):
        session = _make_session()
        save_snapshot(session, ["1", "2", "3", "4", "5"], "abc")
        session.shown_items = ["1", "2", "3"]
        ids = get_next_candidate_ids(session, 3)
        assert ids == ["4", "5"]

    def test_empty_snapshot(self):
        session = _make_session()
        ids = get_next_candidate_ids(session, 3)
        assert ids == []


class TestRecordShown:
    def test_dedup(self):
        session = _make_session()
        record_shown(session, ["1", "2"])
        record_shown(session, ["2", "3"])
        assert session.shown_items == ["1", "2", "3"]

    def test_preserves_order(self):
        session = _make_session()
        record_shown(session, ["3", "1", "2"])
        assert session.shown_items == ["3", "1", "2"]


class TestBrokerDirection:
    def test_set_valid(self):
        session = _make_session(role="broker")
        err = set_broker_direction(session, "search_job")
        assert err is None
        assert session.broker_direction == "search_job"

    def test_non_broker_rejected(self):
        session = _make_session(role="worker")
        err = set_broker_direction(session, "search_job")
        assert err is not None

    def test_invalid_direction(self):
        session = _make_session(role="broker")
        err = set_broker_direction(session, "invalid")
        assert err is not None


class TestFollowUp:
    def test_increment(self):
        session = _make_session()
        assert increment_follow_up(session) == 1
        assert increment_follow_up(session) == 2
