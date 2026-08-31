from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.admin import accounts
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

