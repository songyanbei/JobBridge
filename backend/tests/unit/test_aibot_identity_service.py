from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import aibot_identity_service as service
from app.wecom.identity_client import ConversionResult, IdentityClientError


def _row(**overrides):
    values = dict(
        opaque_actor_digest="d" * 64, bot_id="bot", identity_status="unverified",
        mapped_external_userid=None, canonical_userid=None, resolution_attempts=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_open_userid_success_reaches_verified(monkeypatch):
    db = Mock()
    row = _row()
    svc = service.AibotIdentityService(client=Mock(), bot_id="bot")
    monkeypatch.setattr(svc, "observe_actor", lambda *args, **kwargs: row)
    monkeypatch.setattr(svc, "resolve_open_userids", lambda values: ConversionResult({values[0]: "zhangsan"}, frozenset(), 1))
    binding = SimpleNamespace(binding_id="binding-1")
    monkeypatch.setattr(service, "ensure_binding", lambda *args, **kwargs: binding)
    registered = Mock()
    monkeypatch.setattr(service, "auto_register_worker", registered)
    monkeypatch.setattr(service.settings, "identity_resolution_enabled", True)

    result = svc.resolve_for_event(db, actor_id="open-a", actor_id_kind="open_userid")

    assert result.verified
    assert result.canonical_userid == "zhangsan"
    assert row.identity_status == "verified"
    registered.assert_called_once_with(db, "zhangsan", binding)


def test_open_userid_client_error_returns_pending_without_unbound_exc(monkeypatch):
    db = Mock()
    row = _row()
    svc = service.AibotIdentityService(client=Mock(), bot_id="bot")
    monkeypatch.setattr(svc, "observe_actor", lambda *args, **kwargs: row)
    monkeypatch.setattr(svc, "resolve_open_userids", lambda values: (_ for _ in ()).throw(IdentityClientError("timeout", code="conversion_unavailable", retryable=True)))
    monkeypatch.setattr(service.settings, "identity_resolution_enabled", True)

    result = svc.resolve_for_event(db, actor_id="open-a", actor_id_kind="open_userid")

    assert result.status == "conversion_pending"
    assert result.reason_code == "conversion_unavailable"
    assert row.identity_status == "conversion_pending"
