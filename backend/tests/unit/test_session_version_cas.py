"""Session CAS fencing tests."""
from unittest.mock import MagicMock, patch

import pytest

from app.core.redis_client import (
    UserLockLost,
    delete_session_if_version,
    save_session_if_version,
)
from app.schemas.conversation import SessionState
from app.services import conversation_service


def test_redis_session_cas_passes_expected_version_and_payload():
    redis = MagicMock()
    redis.eval.return_value = 1

    with patch("app.core.redis_client.get_redis", return_value=redis):
        assert save_session_if_version(
            "u-1", {"role": "worker", "session_version": 4}, 3,
        ) is True

    args = redis.eval.call_args.args
    assert args[1:6] == (2, "session:u-1", "__no_user_lock_fence__", 3, 1800)
    assert '"session_version": 4' in args[6]


def test_redis_session_cas_rejects_lost_lock_fence():
    redis = MagicMock()
    redis.eval.return_value = -1
    with patch("app.core.redis_client.get_redis", return_value=redis):
        with pytest.raises(UserLockLost):
            save_session_if_version(
                "u-1", {"session_version": 2}, 1,
                lock_fence=("lock:u-1", "owner-token"),
            )


def test_redis_session_cas_reports_conflict():
    redis = MagicMock()
    redis.eval.return_value = 0
    with patch("app.core.redis_client.get_redis", return_value=redis):
        assert save_session_if_version("u-1", {"session_version": 2}, 1) is False


def test_redis_session_cas_can_restore_missing_durable_state():
    redis = MagicMock()
    redis.eval.return_value = 1
    with patch("app.core.redis_client.get_redis", return_value=redis):
        assert save_session_if_version(
            "u-1",
            {"session_version": 8},
            7,
            allow_missing=True,
        ) is True

    assert redis.eval.call_args.args[-1] == "1"


def test_redis_session_delete_cas_passes_version_and_fence():
    redis = MagicMock()
    redis.eval.return_value = 1
    with patch("app.core.redis_client.get_redis", return_value=redis):
        assert delete_session_if_version(
            "u-1", 7, lock_fence=("lock:u-1", "owner-token"),
        ) is True

    args = redis.eval.call_args.args
    assert args[1:] == (
        2, "session:u-1", "lock:u-1", 7, "owner-token",
    )


def test_conversation_save_increments_version_only_after_success():
    session = SessionState(role="worker", session_version=7)
    with patch.object(
        conversation_service, "redis_save_session_if_version", return_value=True,
    ) as save:
        conversation_service.save_session("u-1", session)

    assert session.session_version == 8
    payload = save.call_args.args[1]
    assert payload["session_version"] == 8
    assert save.call_args.args[2] == 7


def test_conversation_save_rejects_stale_writer_without_mutating_version():
    session = SessionState(role="worker", session_version=7)
    with patch.object(
        conversation_service, "redis_save_session_if_version", return_value=False,
    ):
        with pytest.raises(conversation_service.SessionVersionConflict):
            conversation_service.save_session("u-1", session)

    assert session.session_version == 7


def test_staging_collapses_multiple_saves_into_one_version_increment():
    with patch.object(
        conversation_service,
        "redis_get_session",
        return_value={"session_version": 7},
    ):
        token = conversation_service.begin_session_staging("u-1")

    session = SessionState(role="worker", session_version=7)
    conversation_service.save_session("u-1", session)
    session.search_criteria = {"city": "常州"}
    conversation_service.save_session("u-1", session)
    commit = conversation_service.end_session_staging(token)

    assert commit is not None
    assert commit.expected_version == 7
    assert commit.operation == "save"
    assert commit.payload["session_version"] == 8
    assert commit.payload["search_criteria"] == {"city": "常州"}


def test_staged_delete_wins_over_later_defensive_save():
    with patch.object(
        conversation_service,
        "redis_get_session",
        return_value={"session_version": 3},
    ):
        token = conversation_service.begin_session_staging("u-1")

    conversation_service.clear_session("u-1")
    conversation_service.save_session(
        "u-1", SessionState(role="worker", session_version=3),
    )
    commit = conversation_service.end_session_staging(token)

    assert commit is not None
    assert commit.operation == "delete"
    assert commit.expected_version == 3
    assert commit.payload is None


def test_apply_staged_save_allows_restore_after_redis_ttl_expiry():
    commit = conversation_service.StagedSessionCommit(
        userid="u-1",
        operation="save",
        expected_version=7,
        payload={"role": "worker", "session_version": 8},
    )
    with patch.object(
        conversation_service,
        "redis_save_session_if_version",
        return_value=True,
    ) as save:
        assert conversation_service.apply_staged_session(commit) is True

    assert save.call_args.kwargs["allow_missing"] is True
