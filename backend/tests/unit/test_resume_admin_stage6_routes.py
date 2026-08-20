"""Small HTTP-level RBAC checks for the phase-11 admin surface."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.admin.cleanup import router as cleanup_router
from app.api.admin.config import router as config_router
from app.api.deps import get_db, require_admin_password_changed
from app.core.exceptions import BusinessException
from app.core.responses import fail


def _client(role: str) -> tuple[TestClient, FastAPI]:
    app = FastAPI()

    @app.exception_handler(BusinessException)
    async def handle_business_error(_: Request, exc: BusinessException):
        return JSONResponse(content=fail(exc.code, exc.message, exc.data))

    app.include_router(config_router)
    app.include_router(cleanup_router)
    current = SimpleNamespace(username=f"{role}-user", role=role, password_changed=1)
    app.dependency_overrides[require_admin_password_changed] = lambda: current
    app.dependency_overrides[get_db] = lambda: MagicMock()
    return TestClient(app), app


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_non_super_admin_cannot_mutate_rollout_or_cleanup(role, monkeypatch):
    monkeypatch.setattr(
        "app.services.cleanup_admin_service.redrive_dead_letters",
        lambda *args, **kwargs: pytest.fail("RBAC must run before cleanup mutation"),
    )
    client, app = _client(role)
    try:
        rollout = client.put(
            "/admin/config/resume-replacement-rollout",
            json={"expected_revision": 1, "userids": [], "reason": "replace cohort"},
        )
        redrive = client.post(
            "/admin/cleanup/dead-letters/retry",
            json={"kind": "target", "ids": [1], "reason": "operator request"},
        )
        approve = client.post(
            "/admin/cleanup/media-isolation/1/approve",
            json={"disposition": "detach_reference", "reason": "checked"},
        )
        execute = client.post("/admin/cleanup/media-isolation/1/execute")
    finally:
        client.close()
        app.dependency_overrides.clear()
    assert [response.json()["code"] for response in (rollout, redrive, approve, execute)] == [40301] * 4


@pytest.mark.parametrize("role", ["operator", "super_admin"])
def test_operator_and_super_can_query_rollout_cleanup_and_media(role, monkeypatch):
    monkeypatch.setattr(
        "app.services.resume_replacement_rollout_service.get_allowlist",
        lambda _db: SimpleNamespace(revision=2, userids=("worker-1",)),
    )
    monkeypatch.setattr(
        "app.services.cleanup_admin_service.list_target_tasks",
        lambda *args, **kwargs: [{"id": 1, "status": "dead_letter"}],
    )
    monkeypatch.setattr(
        "app.services.cleanup_admin_service.list_media_issues",
        lambda *args, **kwargs: [{"id": 2, "status": "open"}],
    )
    monkeypatch.setattr(
        "app.services.cleanup_admin_service.list_media_dead_letters",
        lambda *args, **kwargs: [{"id": 3, "status": "dead_letter"}],
    )
    client, app = _client(role)
    try:
        responses = (
            client.get("/admin/config/resume-replacement-rollout"),
            client.get("/admin/cleanup/tasks"),
            client.get("/admin/cleanup/media-isolation"),
            client.get("/admin/cleanup/media-dead-letters"),
        )
    finally:
        client.close()
        app.dependency_overrides.clear()
    assert [response.json()["code"] for response in responses] == [0, 0, 0, 0]
    assert responses[0].json()["data"] == {"revision": 2, "member_count": 1}
    assert "userids" not in responses[0].json()["data"]


def test_viewer_cannot_query_phase6_operator_surfaces():
    client, app = _client("viewer")
    try:
        responses = (
            client.get("/admin/config/resume-replacement-rollout"),
            client.get("/admin/cleanup/tasks"),
            client.get("/admin/cleanup/media-isolation"),
            client.get("/admin/cleanup/media-dead-letters"),
        )
    finally:
        client.close()
        app.dependency_overrides.clear()
    assert [response.json()["code"] for response in responses] == [40301] * 4


def test_rollout_put_requires_non_blank_reason_before_service(monkeypatch):
    monkeypatch.setattr(
        "app.services.resume_replacement_rollout_service.update_allowlist",
        lambda *args, **kwargs: pytest.fail("invalid request must not reach service"),
    )
    client, app = _client("super_admin")
    try:
        missing = client.put(
            "/admin/config/resume-replacement-rollout",
            json={"expected_revision": 1, "userids": []},
        )
        blank = client.put(
            "/admin/config/resume-replacement-rollout",
            json={"expected_revision": 1, "userids": [], "reason": "   "},
        )
    finally:
        client.close()
        app.dependency_overrides.clear()
    assert missing.status_code == 422 and blank.status_code == 422


def test_redrive_http_batch_is_capped_at_fifty_before_service(monkeypatch):
    monkeypatch.setattr(
        "app.services.cleanup_admin_service.redrive_dead_letters",
        lambda *args, **kwargs: pytest.fail("invalid batch must not reach service"),
    )
    client, app = _client("super_admin")
    try:
        response = client.post(
            "/admin/cleanup/dead-letters/retry",
            json={"kind": "target", "ids": list(range(1, 52)), "reason": "too large"},
        )
    finally:
        client.close()
        app.dependency_overrides.clear()
    assert response.status_code == 422


def test_media_redrive_rejects_blank_reason_before_service(monkeypatch):
    monkeypatch.setattr(
        "app.services.cleanup_admin_service.redrive_dead_letters",
        lambda *args, **kwargs: [{"id": 1, "result": "queued"}],
    )
    client, app = _client("super_admin")
    try:
        response = client.post(
            "/admin/cleanup/dead-letters/retry",
            json={"kind": "media", "ids": [1], "reason": "   "},
        )
    finally:
        client.close()
        app.dependency_overrides.clear()
    assert response.status_code == 422
