"""Actor binding is mandatory when replay/claim is requested for an actor."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import action_execution_service as action_service


def _succeeded_row(*, actor_userid=None):
    return SimpleNamespace(
        status="succeeded",
        actor_userid=actor_userid,
        turn_id="turn-1",
        action_name="search_job",
        result_ref_type="terminal",
        result_schema_version="v1",
        request_id=None,
        snapshot_id=None,
        delivery_ids=(),
        outbox_ids=(),
        session_commit_id=None,
    )


def test_load_replay_rejects_unbound_succeeded_row(monkeypatch):
    monkeypatch.setattr(
        action_service,
        "read_action_execution",
        lambda *_args, **_kwargs: _succeeded_row(),
    )

    with pytest.raises(action_service.ActionExecutionStateError, match="actor_binding_missing"):
        action_service.load_replay_reference(
            MagicMock(), "turn-1", "search_job", actor_userid="actor-a",
        )


def test_claim_rejects_unbound_row_instead_of_treating_null_as_wildcard(monkeypatch):
    monkeypatch.setattr(
        action_service,
        "read_action_execution",
        lambda *_args, **_kwargs: _succeeded_row(),
    )

    with pytest.raises(action_service.ActionExecutionConflict, match="actor_binding_missing"):
        action_service.claim_action_execution(
            MagicMock(), "turn-1", "search_job", "worker-a",
            actor_userid="actor-a",
        )

