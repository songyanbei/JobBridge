"""Audit guards for the v1 action execution integration boundary."""

import inspect

from app.models import ActionExecution
from app.services import message_router, worker
from app.listing import search as listing_search
from app.services.action_execution_service import (
    claim_action_execution,
    finalize_action_execution,
    read_action_execution,
)


def test_search_modules_leave_action_lease_to_worker_gateway():
    """Router and Facade stay side-effect free; Worker owns the lease boundary."""
    for module in (message_router, listing_search):
        source = inspect.getsource(module)
        assert "action_execution_service" not in source
        assert "claim_action_execution" not in source
        assert "finalize_action_execution" not in source

    worker_source = inspect.getsource(worker)
    assert "claim_action_execution" in worker_source
    assert "finalize_action_execution" in worker_source
    assert "user_service.identify_or_register(userid, db)" in worker_source
    assert "user_context=preloaded_user_context" in worker_source


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


def test_action_row_stores_replay_references_without_result_payload():
    """Replay uses durable references; full result bodies never belong on the row."""
    assert hasattr(ActionExecution, "result_digest")
    assert hasattr(ActionExecution, "snapshot_id")
    assert hasattr(ActionExecution, "request_id")
    assert hasattr(ActionExecution, "result_schema_version")
    assert hasattr(ActionExecution, "replay_count")
    assert not hasattr(ActionExecution, "result_payload")
    assert all(
        callable(function)
        for function in (
            claim_action_execution,
            finalize_action_execution,
            read_action_execution,
        )
    )
