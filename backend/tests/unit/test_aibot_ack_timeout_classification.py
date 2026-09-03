from unittest.mock import MagicMock

from app.services.aibot_connection import AibotOutboxWriter
from app.wecom.aibot_transport import AibotTransportError


def test_sync_transport_ack_timeout_is_uncertain_not_pending(monkeypatch):
    transport = MagicMock()
    transport.send_outbox.side_effect = AibotTransportError(
        "ack timeout; delivery is uncertain",
    )
    writer = AibotOutboxWriter(transport=transport, lease_owner="owner", fencing_token=1)
    uncertain = []
    pending = []
    monkeypatch.setattr(writer, "_mark_uncertain", lambda item, reason: uncertain.append(reason) or True)
    monkeypatch.setattr(writer, "_mark_pending", lambda item, reason: pending.append(reason) or True)

    assert writer.deliver({"id": 1}) is True
    assert uncertain == ["ack timeout; delivery is uncertain"]
    assert pending == []
