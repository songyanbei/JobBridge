from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models import WecomOutboundOutbox
from app.services.worker import Worker


class _Db:
    def __init__(self, inbound):
        self.inbound = inbound
        self.rows = []

    def get(self, _model, _event_id):
        return self.inbound

    def add(self, row):
        self.rows.append(row)


def test_aibot_outbox_persists_reply_window_from_inbound_time():
    created = datetime(2026, 8, 31, 12, 0, 0)
    db = _Db(SimpleNamespace(
        source_channel="wecom_aibot", conversation_type="single",
        conversation_id="actor", chat_id=None,
        ordering_key="wecom:wecom_aibot:single:actor", provider_req_id="req-1",
        created_at=created,
    ))
    worker = object.__new__(Worker)
    reply = SimpleNamespace(
        userid="actor", msg_type="text", content="ok", intent=None,
        criteria_snapshot=None, recommendation_context=None,
        recommendation_request=None,
    )

    worker._stage_outbox(db, 7, [reply])

    row = db.rows[0]
    assert isinstance(row, WecomOutboundOutbox)
    assert row.reply_expires_at == created + timedelta(hours=24)
    assert row.stream_deadline_at is None


def test_stream_reply_outbox_gets_ten_minute_deadline():
    created = datetime(2026, 8, 31, 12, 0, 0)
    db = _Db(SimpleNamespace(
        source_channel="wecom_aibot", conversation_type="single",
        conversation_id="actor", chat_id=None,
        ordering_key="wecom:wecom_aibot:single:actor", provider_req_id="req-1",
        created_at=created,
    ))
    worker = object.__new__(Worker)
    reply = SimpleNamespace(
        userid="actor", msg_type="text", content="chunk", intent=None,
        criteria_snapshot=None, recommendation_context=None,
        recommendation_request=None, stream_id="stream-1",
    )

    worker._stage_outbox(db, 7, [reply])

    row = db.rows[0]
    assert row.stream_deadline_at == created + timedelta(minutes=10)
