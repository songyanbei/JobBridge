"""AIBot durable inbox/outbox integration contract.

The test is intentionally skipped until the migration implementation exposes
the acceptance/registry APIs.  It then enforces the critical failure semantics
from the migration ADR without depending on a live WSS provider.
"""

from __future__ import annotations

import pytest

from tests.fixtures.aibot_protocol import load_fixture


inbox = pytest.importorskip("app.services.aibot_inbox")
outbox = pytest.importorskip("app.services.aibot_outbox")


def _find(module, names):
    for name in names:
        value = getattr(module, name, None)
        if value is not None:
            return value
    pytest.fail(f"{module.__name__} missing one of {names}")


def test_acceptance_contract_names_durable_result_states():
    result_type = _find(inbox, ("AcceptanceResult", "InboundAcceptanceResult"))
    values = {getattr(item, "value", item) for item in result_type}
    assert {"accepted", "duplicate", "retryable"}.issubset(values)


def test_outbox_status_contract_is_single_authoritative_state():
    statuses = getattr(outbox, "OUTBOX_STATUSES", None)
    if statuses is None:
        statuses = getattr(outbox, "VALID_OUTBOX_STATUSES", None)
    assert statuses is not None
    assert {str(value) for value in statuses} >= {
        "pending", "sending", "sent", "uncertain", "dead_letter",
    }
