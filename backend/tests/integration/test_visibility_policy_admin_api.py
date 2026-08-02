"""HTTP-level RBAC/readiness checks; run with the application test dependencies."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.api.deps import get_db, require_admin_password_changed
from app.main import app
from app.services.visibility_policy import default_policy_document


def test_viewer_http_cannot_save_or_restore_policy():
    viewer = SimpleNamespace(username="viewer", role="viewer", password_changed=1)
    app.dependency_overrides[require_admin_password_changed] = lambda: viewer
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        with TestClient(app) as client:
            body = {
                "policy": default_policy_document(1), "expected_revision": 1,
                "confirm_sensitive_expansion": True,
            }
            save = client.put("/admin/config/visibility-policy", json=body)
            restore = client.post(
                "/admin/config/visibility-policy/history/1/restore",
                json={"expected_revision": 1, "confirm_sensitive_expansion": True},
            )
        assert save.status_code == 200 and save.json()["code"] == 40301
        assert restore.status_code == 200 and restore.json()["code"] == 40301
    finally:
        app.dependency_overrides.clear()
