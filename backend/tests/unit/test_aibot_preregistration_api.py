from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.admin import accounts
from app.models import AibotIdentityAudit
from app.schemas.account import AibotPreRegisterRequest
from app.services import account_service
from app.core.exceptions import BusinessException


def test_preregister_route_is_admin_protected():
    route = next(r for r in accounts.router.routes if r.path.endswith("/pre-register"))
    assert any("require_admin" in repr(dep.call) for dep in route.dependant.dependencies)


def test_preregister_missing_or_revoked_binding_is_rejected():
    db = Mock()
    db.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(BusinessException):
        account_service.pre_register_aibot_for_binding(
            db, binding_id="missing", role="factory", operator="admin",
        )


def test_preregister_creates_minimum_user_and_reuses_pending_registration(monkeypatch):
    db = Mock()
    binding = SimpleNamespace(binding_id="b1", canonical_userid="canonical-1", binding_status="active")
    pending = SimpleNamespace(
        registration_id="r1", registration_status="pending_role", requested_role="factory",
    )
    user = SimpleNamespace(external_userid="canonical-1", role="worker", can_search_jobs=1, can_search_workers=0)
    monkeypatch.setattr(account_service.registration_service, "_ensure_canonical_user", lambda _db, _userid: user)
    # First query is the locked binding, then the existing registration.
    results = [binding, pending]
    def query(_model):
        q = Mock()
        q.filter.return_value = q
        q.first.side_effect = lambda: results.pop(0)
        return q
    db.query.side_effect = query

    result = account_service.pre_register_aibot_for_binding(
        db, binding_id="b1", role="factory", operator="admin",
    )

    assert result is pending
    assert result.registration_status == "pending_role"
    assert result.requested_role == "factory"


def test_preregister_rejects_conflicting_pending_role_and_audits(monkeypatch):
    db = Mock()
    audit_db = Mock()
    monkeypatch.setattr(account_service, "SessionLocal", lambda: audit_db)
    binding = SimpleNamespace(
        binding_id="b1",
        bot_id="bot-1",
        opaque_actor_digest="d" * 64,
        canonical_userid="canonical-1",
        binding_status="active",
    )
    pending = SimpleNamespace(
        registration_id="r1", registration_status="pending_role", requested_role="factory",
    )
    user = SimpleNamespace(external_userid="canonical-1", role="worker", can_search_jobs=1, can_search_workers=0)
    monkeypatch.setattr(account_service.registration_service, "_ensure_canonical_user", lambda _db, _userid: user)
    db.query.return_value.filter.return_value.first.return_value = pending

    with pytest.raises(BusinessException) as exc_info:
        account_service.pre_register_aibot(
            db, binding=binding, role="broker", operator="admin",
        )

    assert exc_info.value.code == 40904
    assert "冲突" in exc_info.value.message
    assert pending.requested_role == "factory"
    audit = audit_db.add.call_args.args[0]
    assert audit.action == "registration_role_conflict"
    assert audit.result == "rejected"
    assert audit.reason_code == "pending_role_conflict"
    assert audit.audit_metadata["existing_role"] == "factory"
    assert audit.audit_metadata["requested_role"] == "broker"
    audit_db.commit.assert_called_once_with()
    audit_db.close.assert_called_once_with()


def test_preregister_route_propagates_role_conflict_and_rolls_back(monkeypatch):
    db = Mock()
    monkeypatch.setattr(
        account_service,
        "pre_register_aibot_for_binding",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            BusinessException(40904, "已有待审核角色申请，申请角色冲突"),
        ),
    )

    with pytest.raises(BusinessException) as exc_info:
        accounts.pre_register_aibot_binding(
            "b1",
            AibotPreRegisterRequest(role="broker"),
            db=db,
            current=SimpleNamespace(username="admin"),
        )

    assert exc_info.value.code == 40904
    db.rollback.assert_called_once_with()


def test_preregister_route_conflict_audit_survives_outer_rollback(monkeypatch):
    class _AuditSession:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

        def commit(self):
            return None

        def rollback(self):
            self.rows.clear()

        def close(self):
            return None

        def query(self, _model):
            query = Mock()
            query.filter.return_value = query
            query.first.return_value = self.rows[0] if self.rows else None
            return query

    db = Mock()
    binding = SimpleNamespace(
        binding_id="b1",
        bot_id="bot-1",
        opaque_actor_digest="d" * 64,
        canonical_userid="canonical-1",
        binding_status="active",
    )
    pending = SimpleNamespace(
        registration_id="r1", registration_status="pending_role", requested_role="factory",
    )
    user = SimpleNamespace(external_userid="canonical-1", role="worker", can_search_jobs=1, can_search_workers=0)
    monkeypatch.setattr(account_service.registration_service, "_ensure_canonical_user", lambda _db, _userid: user)
    results = [binding, pending]

    def query(_model):
        query = Mock()
        query.filter.return_value = query
        query.first.side_effect = lambda: results.pop(0)
        return query

    db.query.side_effect = query
    audit_db = _AuditSession()
    monkeypatch.setattr(account_service, "SessionLocal", lambda: audit_db)

    with pytest.raises(BusinessException) as exc_info:
        accounts.pre_register_aibot_binding(
            "b1",
            AibotPreRegisterRequest(role="broker"),
            db=db,
            current=SimpleNamespace(username="admin"),
        )

    assert exc_info.value.code == 40904
    db.rollback.assert_called_once_with()
    saved = audit_db.query(AibotIdentityAudit).filter(
        AibotIdentityAudit.action == "registration_role_conflict",
    ).first()
    assert saved is not None
    assert saved.result == "rejected"
    assert saved.reason_code == "pending_role_conflict"
