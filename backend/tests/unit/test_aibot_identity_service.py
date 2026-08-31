from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import aibot_identity_service as service
from app.services import registration_service
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


def test_plain_directory_outage_returns_pending_for_worker_retry(monkeypatch):
    db = Mock()
    row = _row()
    svc = service.AibotIdentityService(plain_verifier=lambda _userid: (False, "directory_unavailable"), bot_id="bot")
    monkeypatch.setattr(svc, "observe_actor", lambda *args, **kwargs: row)

    result = svc.resolve_for_event(db, actor_id="canonical-a", actor_id_kind="plain")

    assert result.status == "conversion_pending"
    assert result.reason_code == "directory_unavailable"
    assert row.identity_status == "conversion_pending"
    assert row.resolution_attempts == 1
    assert row.next_resolution_at is not None
    assert db.add.call_args.args[0].action == "directory_verify"
    assert db.add.call_args.args[0].result == "pending"


def test_open_userid_directory_outage_returns_pending_for_worker_retry(monkeypatch):
    db = Mock()
    row = _row()
    svc = service.AibotIdentityService(
        client=Mock(),
        plain_verifier=lambda _userid: (False, "directory_unavailable"),
        bot_id="bot",
    )
    monkeypatch.setattr(svc, "observe_actor", lambda *args, **kwargs: row)
    monkeypatch.setattr(
        svc,
        "resolve_open_userids",
        lambda values: ConversionResult({values[0]: "zhangsan"}, frozenset(), 1),
    )
    monkeypatch.setattr(service.settings, "identity_resolution_enabled", True)

    result = svc.resolve_for_event(db, actor_id="open-a", actor_id_kind="open_userid")

    assert result.status == "conversion_pending"
    assert result.reason_code == "directory_unavailable"
    assert row.identity_status == "conversion_pending"
    assert row.resolution_attempts == 1
    assert row.next_resolution_at is not None
    audit = db.add.call_args.args[0]
    assert audit.action == "directory_verify"
    assert audit.result == "pending"
    assert audit.canonical_userid == "zhangsan"


def test_nonretryable_identity_client_error_is_rejected(monkeypatch):
    db = Mock()
    row = _row()
    svc = service.AibotIdentityService(client=Mock(), bot_id="bot")
    monkeypatch.setattr(svc, "observe_actor", lambda *args, **kwargs: row)
    monkeypatch.setattr(svc, "resolve_open_userids", lambda values: (_ for _ in ()).throw(IdentityClientError("bad code", code="unknown_errcode", retryable=False)))
    monkeypatch.setattr(service.settings, "identity_resolution_enabled", True)

    result = svc.resolve_for_event(db, actor_id="open-a", actor_id_kind="open_userid")

    assert result.status == "rejected"
    assert result.reason_code == "unknown_errcode"
    assert row.next_resolution_at is None


def test_provider_5xx_identity_error_stays_pending(monkeypatch):
    db = Mock()
    row = _row()
    svc = service.AibotIdentityService(client=Mock(), bot_id="bot")
    monkeypatch.setattr(svc, "observe_actor", lambda *args, **kwargs: row)
    monkeypatch.setattr(svc, "resolve_open_userids", lambda values: (_ for _ in ()).throw(IdentityClientError("provider outage", code="conversion_err_50002", retryable=True)))
    monkeypatch.setattr(service.settings, "identity_resolution_enabled", True)

    result = svc.resolve_for_event(db, actor_id="open-a", actor_id_kind="open_userid")

    assert result.status == "conversion_pending"
    assert row.last_error_code == "conversion_err_50002"
    assert row.next_resolution_at is not None


def test_binding_ensures_canonical_user_before_flush(monkeypatch):
    db = Mock()
    calls = []
    canonical_user = SimpleNamespace(external_userid="zhangsan", role="worker", can_search_jobs=1, can_search_workers=0)
    monkeypatch.setattr(registration_service, "_ensure_canonical_user", lambda _db, userid: calls.append(("user", userid)) or canonical_user)
    db.query.return_value.filter.return_value.first.return_value = None
    db.flush.side_effect = lambda: calls.append(("flush", "binding"))

    binding = registration_service.ensure_binding(
        db, bot_id="bot", opaque_actor_digest_value="d" * 64, canonical_userid="zhangsan",
    )

    assert binding.canonical_userid == "zhangsan"
    assert calls[0] == ("user", "zhangsan")
    assert calls.index(("user", "zhangsan")) < calls.index(("flush", "binding"))


def test_revoked_identity_replay_is_rejected_without_conversion(monkeypatch):
    db = Mock()
    row = _row(identity_status="revoked", revoked_at="now")
    svc = service.AibotIdentityService(client=Mock(), bot_id="bot")
    monkeypatch.setattr(svc, "observe_actor", lambda *args, **kwargs: row)
    convert = Mock()
    monkeypatch.setattr(svc, "resolve_open_userids", convert)

    result = svc.resolve_for_event(db, actor_id="open-a", actor_id_kind="open_userid")

    assert result.status == "revoked"
    assert result.reason_code == "identity_revoked"
    convert.assert_not_called()


def test_revoke_binding_marks_identity_revoked(monkeypatch):
    db = Mock()
    binding = SimpleNamespace(
        binding_id="b1", bot_id="bot", opaque_actor_digest="d" * 64,
        canonical_userid="zhangsan", binding_status="active",
    )
    registration = SimpleNamespace(registration_status="active")
    identity = SimpleNamespace(identity_status="verified", revoked_at=None, last_error_code=None)

    def query(model):
        q = Mock()
        q.filter.return_value = q
        if model is registration_service.AibotIdentityBinding:
            q.first.return_value = binding
        elif model is registration_service.AibotRegistration:
            q.first.return_value = registration
        else:
            q.first.return_value = identity
        return q
    db.query.side_effect = query

    registration_service.revoke_binding(db, binding_id="b1", operator="admin", reason="security")

    assert binding.binding_status == "revoked"
    assert registration.registration_status == "revoked"
    assert identity.identity_status == "revoked"
    assert identity.revoked_at is not None


def test_repeated_invite_apply_is_idempotent_without_consuming_use():
    from datetime import datetime, timedelta

    db = Mock()
    binding = SimpleNamespace(binding_id="b1", bot_id="bot", opaque_actor_digest="d" * 64, canonical_userid="u1", binding_status="active")
    invite = SimpleNamespace(target_role="factory", revoked_at=None, expires_at=datetime.utcnow() + timedelta(hours=1), used_count=0, max_uses=1, invite_id="i1")
    registration = SimpleNamespace(registration_status="pending_role", requested_role="factory")
    queries = [invite, None, invite, registration]

    def query(_model):
        q = Mock()
        q.filter.return_value = q
        q.first.side_effect = lambda: queries.pop(0)
        return q
    db.query.side_effect = query

    first = registration_service.apply_invite(db, binding=binding, token="token")
    second = registration_service.apply_invite(db, binding=binding, token="token")

    assert first.requested_role == "factory"
    assert second is registration
    assert invite.used_count == 1
