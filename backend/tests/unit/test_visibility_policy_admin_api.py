"""HTTP-level visibility policy RBAC tests that run in ordinary unit CI."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.admin.config import router
from app.api.deps import get_db, require_admin_password_changed
from app.core.exceptions import BusinessException
from app.core.responses import fail
from app.services.visibility_policy import default_policy_document


def _viewer_client() -> tuple[TestClient, FastAPI]:
    test_app = FastAPI()

    @test_app.exception_handler(BusinessException)
    async def handle_business_error(_: Request, exc: BusinessException):
        return JSONResponse(content=fail(exc.code, exc.message, exc.data))

    test_app.include_router(router)
    viewer = SimpleNamespace(username="viewer", role="viewer", password_changed=1)
    test_app.dependency_overrides[require_admin_password_changed] = lambda: viewer
    test_app.dependency_overrides[get_db] = lambda: MagicMock()
    return TestClient(test_app), test_app


def test_viewer_http_cannot_save_or_restore_policy():
    client, test_app = _viewer_client()
    try:
        body = {
            "policy": default_policy_document(1), "expected_revision": 1,
            "confirm_sensitive_expansion": True,
        }
        save = client.put("/admin/config/visibility-policy", json=body)
        restore = client.post(
            "/admin/config/visibility-policy/history/1/restore",
            json={"expected_revision": 1, "confirm_sensitive_expansion": True},
        )
    finally:
        client.close()
        test_app.dependency_overrides.clear()
    assert save.status_code == 200 and save.json()["code"] == 40301
    assert restore.status_code == 200 and restore.json()["code"] == 40301
