from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.worker import Worker
from app.wecom.callback import WeComMessage


class _MediaDb:
    def __init__(self, row):
        self.row = row
        self.updated = None

    def get(self, _model, _event_id):
        return self.row

    def query(self, _model):
        query = MagicMock()
        query.filter.return_value.update.side_effect = self._update
        return query

    def _update(self, values, **_kwargs):
        self.updated = values
        return 1

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _message():
    return WeComMessage(
        msg_id="aibot_internal",
        from_user="opaque-user",
        msg_type="image",
        media_id="legacy-media-id",
        source_channel="wecom_aibot",
    )


def test_aibot_worker_downloads_url_and_persists_storage_ref():
    row = SimpleNamespace(
        media_storage_ref=None,
        media_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
        media_download_attempts=0,
        provider_msg_id="provider-1",
        msg_id="aibot_internal",
        source_channel="wecom_aibot",
        media_url_ciphertext=b"url-cipher",
        media_aes_key_ciphertext=b"aes-cipher",
    )
    db = _MediaDb(row)
    response = SimpleNamespace(content=b"bytes", raise_for_status=lambda: None)
    storage = MagicMock()

    class Crypto:
        @classmethod
        def from_settings(cls):
            return cls()

        def decrypt(self, value, **kwargs):
            assert kwargs["entity_id"] == "provider-1"
            return "https://example.invalid/media" if value == b"url-cipher" else "aes-key"

    worker = object.__new__(Worker)
    msg = _message()
    with patch("app.services.worker.SessionLocal", return_value=db), \
        patch("app.services.pii_crypto_service.PiiCryptoService", Crypto), \
        patch("httpx.get", return_value=response) as get, \
        patch("app.storage.get_storage", return_value=storage):
        worker._download_and_attach_aibot_media(msg, 7)

    get.assert_called_once_with("https://example.invalid/media", timeout=10.0)
    storage.save.assert_called_once()
    assert msg.media_storage_ref.startswith("aibot-media/wecom_aibot/provider-1")
    assert msg.image_url == msg.media_storage_ref
    assert db.updated["media_download_status"] == "downloaded"


def test_expired_aibot_media_is_terminal_and_not_downloaded():
    row = SimpleNamespace(
        media_storage_ref=None,
        media_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
        media_download_attempts=2,
        provider_msg_id="provider-2",
        msg_id="aibot_internal",
        source_channel="wecom_aibot",
        media_url_ciphertext=b"url-cipher",
        media_aes_key_ciphertext=b"aes-cipher",
    )
    db = _MediaDb(row)
    worker = object.__new__(Worker)
    with patch("app.services.worker.SessionLocal", return_value=db), \
        patch("httpx.get") as get:
        worker._download_and_attach_aibot_media(_message(), 7)

    get.assert_not_called()
    assert row.media_download_status == "expired"
    assert row.media_download_attempts == 3


def test_aibot_download_failure_is_propagated_for_bounded_retry():
    row = SimpleNamespace(
        media_storage_ref=None,
        media_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
        media_download_attempts=0,
        provider_msg_id="provider-3",
        msg_id="aibot_internal",
        source_channel="wecom_aibot",
        media_url_ciphertext=b"url-cipher",
        media_aes_key_ciphertext=b"aes-cipher",
    )
    db = _MediaDb(row)
    response = SimpleNamespace(content=b"", raise_for_status=MagicMock(side_effect=RuntimeError("download failed")))

    class Crypto:
        @classmethod
        def from_settings(cls):
            return cls()

        def decrypt(self, value, **_kwargs):
            return "https://example.invalid/media" if value == b"url-cipher" else "aes-key"

    worker = object.__new__(Worker)
    with patch("app.services.worker.SessionLocal", return_value=db), \
        patch("app.services.pii_crypto_service.PiiCryptoService", Crypto), \
        patch("httpx.get", return_value=response):
        try:
            worker._download_and_attach_aibot_media(_message(), 7)
        except RuntimeError as exc:
            assert str(exc) == "download failed"
        else:
            raise AssertionError("download failure must be retryable")
