from unittest.mock import MagicMock, patch

from app.services.aibot_connection import AibotOutboxWriter


def _query_chain(update_result):
    query = MagicMock()
    query.filter.return_value = query
    query.update.return_value = update_result
    return query


def test_recover_stale_splits_prewrite_and_written_claims():
    expired_query = _query_chain(0)
    pending_query = _query_chain(1)
    uncertain_query = _query_chain(1)
    delivery_query = _query_chain(1)
    db = MagicMock()
    db.query.side_effect = [expired_query, pending_query, uncertain_query, delivery_query]
    with patch("app.services.aibot_connection.SessionLocal", return_value=db):
        writer = AibotOutboxWriter(transport=MagicMock(), lease_owner="owner", fencing_token=7)
        assert writer.recover_stale() == 2
    assert pending_query.update.call_args.args[0]["status"] == "pending"
    assert uncertain_query.update.call_args.args[0]["status"] == "uncertain"
    pending_filter = " ".join(str(value) for value in pending_query.filter.call_args.args)
    uncertain_filter = " ".join(str(value) for value in uncertain_query.filter.call_args.args)
    assert "first_sent_at" in pending_filter
    assert "first_sent_at" in uncertain_filter
    assert delivery_query.update.call_args.args[0]["status"] == "unknown"


def test_frame_write_callback_persists_first_sent_at_before_ack():
    writer = AibotOutboxWriter(transport=MagicMock(), lease_owner="owner", fencing_token=7)
    with patch.object(writer, "_mark_frame_written", return_value=True) as mark:
        callback = lambda: writer._mark_frame_written({"id": 12})
        callback()
    mark.assert_called_once_with({"id": 12})
