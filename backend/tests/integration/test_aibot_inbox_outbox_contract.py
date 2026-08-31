"""AIBot durable inbox/outbox integration contract.

The contract is intentionally bound to the durable acceptance service and ORM
model used by production.  It does not require a live WSS, MySQL, or Redis.
"""

from __future__ import annotations

from typing import get_args

from app.models import WecomOutboundOutbox
from app.services.inbound_acceptance import AcceptanceStatus, InboundAcceptanceService


def test_acceptance_contract_names_durable_result_states():
    values = set(get_args(AcceptanceStatus))
    assert {"accepted", "duplicate", "retryable"}.issubset(values)
    assert InboundAcceptanceService.__name__ == "InboundAcceptanceService"


def test_outbox_status_contract_is_single_authoritative_state():
    statuses = WecomOutboundOutbox.__table__.columns["status"].type.enums
    assert set(statuses) >= {
        "pending", "sending", "sent", "uncertain", "dead_letter",
    }


def test_outbox_ack_contract_has_fencing_and_uncertainty_columns():
    columns = WecomOutboundOutbox.__table__.columns
    assert {"ack_req_id", "ack_received_at", "uncertain_at", "fencing_token"}.issubset(columns.keys())
