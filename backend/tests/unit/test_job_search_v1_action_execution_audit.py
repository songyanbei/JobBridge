"""Audit guards for the intentionally-unintegrated v1 action execution contract."""

import inspect

from app.models import ActionExecution
from app.services import message_router, worker
from app.listing import search as listing_search
from app.services.action_execution_service import (
    claim_action_execution,
    finalize_action_execution,
    read_action_execution,
)


def test_search_production_modules_do_not_partially_integrate_action_execution():
    """A partial claim/finalize wrapper would create un-replayable facts."""
    for module in (worker, message_router, listing_search):
        source = inspect.getsource(module)
        assert "action_execution_service" not in source
        assert "claim_action_execution" not in source
        assert "finalize_action_execution" not in source


def test_turn_id_reaches_worker_router_message_boundary():
    msg = worker._build_wecom_message(
        {
            "msg_id": "msg-audit",
            "turn_id": "turn-audit",
            "from_userid": "worker-1",
            "msg_type": "text",
            "content": "深圳 普工",
        },
    )

    assert msg.turn_id == "turn-audit"


def test_session_cas_is_after_the_business_db_commit():
    source = inspect.getsource(worker.Worker._process_locked)
    business_commit = source.index("self._write_conversation_log")
    db_commit = source.index("db.commit()", business_commit)
    session_apply = source.index("self._apply_session_commit_for_event", db_commit)

    assert db_commit < session_apply


def test_action_row_cannot_replay_a_search_snapshot_by_itself():
    """The current row stores only a digest, which is the replay blocker."""
    assert hasattr(ActionExecution, "result_digest")
    assert not hasattr(ActionExecution, "snapshot_id")
    assert not hasattr(ActionExecution, "result_payload")
    assert all(
        callable(function)
        for function in (
            claim_action_execution,
            finalize_action_execution,
            read_action_execution,
        )
    )
