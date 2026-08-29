from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import jobs, resumes
from app.services import audit_workbench_service


def _dto_stub(images):
    dumped = SimpleNamespace(model_dump=lambda **_: {"images": images})
    return SimpleNamespace(model_validate=lambda _: dumped)


def test_admin_job_and_resume_dtos_project_media_urls(monkeypatch):
    project_job = MagicMock(return_value=["/files/jobs/a.jpg"])
    project_resume = MagicMock(return_value=["/files/resumes/a.jpg"])
    monkeypatch.setattr(jobs, "JobRead", _dto_stub(["jobs/a.jpg"]))
    monkeypatch.setattr(resumes, "ResumeRead", _dto_stub(["resumes/a.jpg"]))
    monkeypatch.setattr(jobs, "storage_urls_for_response", project_job)
    monkeypatch.setattr(resumes, "storage_urls_for_response", project_resume)

    job = SimpleNamespace(owner_userid="factory-1")
    resume = SimpleNamespace(owner_userid="worker-1")
    assert jobs._job_to_dict(job, {})["images"] == ["/files/jobs/a.jpg"]
    assert resumes._resume_to_dict(resume, {})["images"] == ["/files/resumes/a.jpg"]
    project_job.assert_called_once_with(["jobs/a.jpg"])
    project_resume.assert_called_once_with(["resumes/a.jpg"])


def test_audit_detail_projects_media_urls(monkeypatch):
    obj = SimpleNamespace(
        id=7,
        version=1,
        owner_userid="worker-1",
        raw_text="resume",
        description="resume",
        extra={},
        images=["resumes/a.jpg"],
        audit_status="pending",
        audit_reason=None,
        audited_by=None,
        audited_at=None,
        created_at=None,
        expires_at=None,
    )
    monkeypatch.setattr(audit_workbench_service, "_load", lambda *_: obj)
    monkeypatch.setattr(audit_workbench_service, "_risk_level", lambda *_: ("low", []))
    monkeypatch.setattr(audit_workbench_service, "get_audit_lock_holder", lambda *_: None)
    monkeypatch.setattr(audit_workbench_service, "_submitter_history", lambda *_: [])
    project = MagicMock(return_value=["/files/resumes/a.jpg"])
    monkeypatch.setattr(
        "app.services.storage_reference_service.storage_urls_for_response", project,
    )

    detail = audit_workbench_service.get_detail(MagicMock(), "resume", 7)

    assert detail["images"] == ["/files/resumes/a.jpg"]
    project.assert_called_once_with(["resumes/a.jpg"])


def test_local_media_mount_serves_only_files_below_storage_root(tmp_path):
    from app.main import mount_local_storage_files

    upload_root = tmp_path / "uploads"
    image_path = upload_root / "images" / "user" / "a.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"jobbridge-image")
    (tmp_path / "secret.txt").write_text("not-public", encoding="utf-8")
    test_app = FastAPI()
    mount_local_storage_files(
        test_app,
        provider="local",
        url_prefix="/files",
        directory=str(upload_root),
    )

    client = TestClient(test_app)
    response = client.get("/files/images/user/a.jpg")

    assert response.status_code == 200
    assert response.content == b"jobbridge-image"
    assert client.get("/files/%2e%2e%2fsecret.txt").status_code == 404
