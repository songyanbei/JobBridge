from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.api.deps import get_db, require_admin_password_changed
from app.core.exceptions import BusinessException
from app.main import app
from app.services import system_config_service as service
from app.services.visibility_policy import default_policy_document


def _item(revision: int = 1):
    return SimpleNamespace(
        config_key=service.VISIBILITY_POLICY_KEY,
        config_value=json.dumps(default_policy_document(revision)),
        value_type="json", updated_by=None,
    )


def _db(item, logs=None):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = item
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = item
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = logs or []
    return db


def test_generic_update_cannot_overwrite_policy():
    db = _db(_item())
    with pytest.raises(ValueError, match="config_locked"):
        service.update(db, service.VISIBILITY_POLICY_KEY, "{}", "json", "admin")


def test_invalid_policy_does_not_mutate_or_commit():
    item = _item()
    db = _db(item)
    bad = default_policy_document(1)
    bad["job_search"]["worker"] = ["job_category", "salary"]
    with pytest.raises(BusinessException) as exc:
        service.update_visibility_policy(db, bad, 1, "admin")
    assert exc.value.code == 40101
    assert json.loads(item.config_value)["revision"] == 1
    db.commit.assert_not_called()


def test_sensitive_expansion_requires_confirmation_and_revision_conflict():
    item = _item()
    current = default_policy_document(1)
    current["job_search"]["broker"].remove("phone")
    item.config_value = json.dumps(current)
    db = _db(item)
    expanded = default_policy_document(1)
    expanded["job_search"]["worker"] = ["hiring_company", "job_category", "salary"]
    with pytest.raises(BusinessException) as exc:
        service.update_visibility_policy(db, expanded, 1, "admin")
    assert exc.value.data["confirm_required"] is True
    with pytest.raises(BusinessException) as exc:
        service.update_visibility_policy(db, expanded, 0, "admin", True)
    assert exc.value.code == 40902


def test_success_increments_revision_and_writes_normalized_audit():
    item = _item()
    db = _db(item)
    policy = default_policy_document(999)
    policy.pop("schema_version")
    policy.pop("revision")
    with patch.object(service, "write_admin_log") as write_log:
        result = service.update_visibility_policy(db, policy, 1, "admin")
    assert result["revision"] == 2
    assert json.loads(item.config_value)["revision"] == 2
    write_log.assert_called_once()
    assert write_log.call_args.kwargs["after"]["revision"] == 2
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/admin/config/visibility-policy", {
            "policy": default_policy_document(1), "expected_revision": 1,
            "confirm_sensitive_expansion": True,
        }),
        ("post", "/admin/config/visibility-policy/history/1/restore", {
            "expected_revision": 1, "confirm_sensitive_expansion": True,
        }),
    ],
)
def test_viewer_cannot_save_or_restore_policy(method, path, body):
    viewer = SimpleNamespace(
        id=1, username="viewer", role="viewer", enabled=1, password_changed=1,
    )
    app.dependency_overrides[require_admin_password_changed] = lambda: viewer
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        response = getattr(TestClient(app), method)(path, json=body)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["code"] == 40301


def test_integrity_requires_matching_complete_success_audit():
    policy = default_policy_document(2)
    item = _item(2)
    audit = SimpleNamespace(
        id=9, operator="admin", created_at=None,
        snapshot={"after": {"config_value": policy, "revision": 2, "schema_version": 1}},
    )
    policy_query, audit_query, ttl_query = MagicMock(), MagicMock(), MagicMock()
    policy_query.filter.return_value.first.return_value = item
    audit_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [audit]
    ttl_query.filter.return_value.first.return_value = SimpleNamespace(config_value="180")
    db = MagicMock()
    db.query.side_effect = [policy_query, audit_query, ttl_query]
    assert service.check_visibility_policy_integrity(db) == {
        "ok": True, "revision": 2, "audit_id": 9,
    }


def test_integrity_fails_when_active_revision_audit_is_missing():
    policy_query, audit_query, ttl_query = MagicMock(), MagicMock(), MagicMock()
    policy_query.filter.return_value.first.return_value = _item(2)
    audit_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    ttl_query.filter.return_value.first.return_value = SimpleNamespace(config_value="180")
    db = MagicMock()
    db.query.side_effect = [policy_query, audit_query, ttl_query]
    result = service.check_visibility_policy_integrity(db)
    assert result["ok"] is False
    assert result["error"] == "active_revision_success_audit_missing"


def test_readiness_returns_503_but_liveness_stays_200(monkeypatch):
    monkeypatch.setattr(main_module, "_readiness_report", lambda: {
        "status": "not_ready", "db": {"ok": True},
        "visibility_policy": {"ok": False, "error": "visibility_policy_missing"},
    })
    client = TestClient(app)
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert client.get("/health").status_code == 200
