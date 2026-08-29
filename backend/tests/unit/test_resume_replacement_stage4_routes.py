"""Minimal HTTP/RBAC contract for stage-4 replacement management routes."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.admin.resumes import router
from app.api.deps import get_db, require_admin_password_changed
from app.core.exceptions import BusinessException
from app.core.responses import fail


def _client(role: str):
    app = FastAPI()

    @app.exception_handler(BusinessException)
    async def handle_business_error(_: Request, exc: BusinessException):
        return JSONResponse(content=fail(exc.code, exc.message, exc.data))

    app.include_router(router)
    db = MagicMock()
    app.dependency_overrides[require_admin_password_changed] = lambda: SimpleNamespace(
        username=f"{role}-1", role=role, password_changed=1,
    )
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), app, db


def test_viewer_cannot_cancel_or_retry_replacement():
    client, app, _db = _client("viewer")
    try:
        cancel = client.post("/admin/resumes/replacements/9/cancel", json={"reason": "duplicate"})
        retry = client.post(
            "/admin/resumes/replacements/9/retry",
            json={"old_resume_version": 3, "reason": "conflict resolved"},
        )
    finally:
        client.close()
        app.dependency_overrides.clear()

    assert cancel.status_code == 200 and cancel.json()["code"] == 40301
    assert retry.status_code == 200 and retry.json()["code"] == 40301


def test_operator_retry_calls_service_once_and_commits():
    client, app, db = _client("operator")
    try:
        with patch("app.services.resume_replace_service.retry_activation", return_value=True) as retry:
            response = client.post(
                "/admin/resumes/replacements/9/retry",
                json={"old_resume_version": 3, "reason": "conflict resolved"},
            )
    finally:
        client.close()
        app.dependency_overrides.clear()

    assert response.status_code == 200 and response.json()["data"] == {"activated": True}
    retry.assert_called_once_with(
        db, 9, 3, operator="operator-1", reason="conflict resolved",
    )
    db.commit.assert_called_once_with()
