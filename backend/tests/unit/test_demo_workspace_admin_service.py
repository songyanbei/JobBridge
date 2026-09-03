from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import AuditLog, DemoPrincipal, DemoResource, DemoWorkspace, DemoWorkspaceMember, User
from app.services import demo_mode_service
from app.services import demo_workspace_admin_service as admin_service
from app.api.admin.demo import DemoMemberRequest, grant_demo_member, revoke_demo_member_delete, router


class _FakeRedis:
    def __init__(self):
        self.deleted = []

    def scan_iter(self, match=None):
        return iter([])

    def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    # User/AuditLog use MySQL-only unsigned types in production. Keep this
    # test portable while retaining the relevant FK constraints.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "PRAGMA foreign_keys=ON"
        )
        conn.exec_driver_sql(
            "CREATE TABLE user (external_userid VARCHAR(64) PRIMARY KEY, role VARCHAR(16) NOT NULL, "
            "display_name VARCHAR(64), company VARCHAR(128), address VARCHAR(255), "
            "contact_person VARCHAR(64), phone VARCHAR(32), phone_ciphertext BLOB, "
            "phone_key_version INTEGER, phone_digest VARCHAR(64), contact_person_ciphertext BLOB, "
            "contact_person_key_version INTEGER, contact_person_digest VARCHAR(64), "
            "wechat_ciphertext BLOB, wechat_key_version INTEGER, wechat_digest VARCHAR(64), "
            "can_search_jobs INTEGER NOT NULL DEFAULT 0, can_search_workers INTEGER NOT NULL DEFAULT 0, "
            "status VARCHAR(16) NOT NULL DEFAULT 'active', blocked_reason VARCHAR(255), "
            "registered_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_active_at DATETIME, extra JSON)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "target_type VARCHAR(32) NOT NULL, target_id VARCHAR(64) NOT NULL, "
            "action VARCHAR(32) NOT NULL, reason VARCHAR(255), operator VARCHAR(64), "
            "snapshot JSON, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
    Base.metadata.create_all(
        engine,
        tables=[DemoWorkspace.__table__, DemoWorkspaceMember.__table__,
                DemoPrincipal.__table__, DemoResource.__table__],
    )

    fake_redis = _FakeRedis()
    monkeypatch.setattr(admin_service, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(admin_service.settings, "app_env", "test")
    monkeypatch.setattr(admin_service.settings, "demo_mode_enabled", True)
    monkeypatch.setattr(admin_service.settings, "demo_allowed_bot_ids", "bot-1")
    monkeypatch.setattr(admin_service.settings, "demo_allowed_actor_digests", "")
    with Session(engine) as session:
        yield session


def test_cleanup_removes_principals_before_synthetic_users(db, monkeypatch):
    actor_digest = demo_mode_service.actor_digest_for_lookup("actor")
    monkeypatch.setattr(admin_service.settings, "demo_allowed_actor_digests", actor_digest)
    workspace = demo_mode_service.create_workspace(
        db, name="demo", bot_id="bot-1", actor_digest_value=actor_digest,
        canonical_actor_userid="real-user", created_by="admin", demo_id="demo-fk-order",
    )
    db.commit()

    result = admin_service.cleanup_workspace(
        db, demo_id=workspace.demo_id, reason="test cleanup", operator="admin",
    )

    assert result["status"] == "cleaned"
    assert db.query(DemoPrincipal).filter_by(demo_id=workspace.demo_id).count() == 0
    assert db.query(User).filter(User.external_userid.like("demo_%")).count() == 0
    assert db.query(DemoWorkspace).filter_by(demo_id=workspace.demo_id).one().status == "cleaned"


def test_cleanup_is_idempotent_after_completion(db, monkeypatch):
    actor_digest = demo_mode_service.actor_digest_for_lookup("actor-idempotent")
    monkeypatch.setattr(admin_service.settings, "demo_allowed_actor_digests", actor_digest)
    workspace = demo_mode_service.create_workspace(
        db, name="demo", bot_id="bot-1", actor_digest_value=actor_digest,
        created_by="admin", demo_id="demo-idempotent",
    )
    db.commit()
    first = admin_service.cleanup_workspace(
        db, demo_id=workspace.demo_id, reason="first", operator="admin",
    )
    second = admin_service.cleanup_workspace(
        db, demo_id=workspace.demo_id, reason="second", operator="admin",
    )
    assert first["status"] == second["status"] == "cleaned"


def test_admin_can_grant_and_revoke_member_without_plaintext_actor(db, monkeypatch):
    owner_digest = demo_mode_service.actor_digest_for_lookup("owner")
    monkeypatch.setattr(admin_service.settings, "demo_allowed_actor_digests", owner_digest)
    workspace = demo_mode_service.create_workspace(
        db, name="demo", bot_id="bot-1", actor_digest_value=owner_digest,
        created_by="admin", demo_id="demo-members",
    )
    db.commit()
    member_digest = demo_mode_service.actor_digest_for_lookup("another-actor")
    req = DemoMemberRequest(
        bot_id="bot-1", actor_digest=member_digest,
        canonical_actor_userid="another-real-user",
    )
    result = grant_demo_member(
        workspace.demo_id, req, db, SimpleNamespace(username="admin"),
    )
    assert result["data"]["status"] == "active"
    assert db.query(DemoWorkspaceMember).filter_by(
        demo_id=workspace.demo_id, opaque_actor_digest=member_digest,
    ).one().membership_status == "active"

    revoke_demo_member_delete(
        workspace.demo_id, member_digest, db, SimpleNamespace(username="admin"),
    )
    assert db.query(DemoWorkspaceMember).filter_by(
        demo_id=workspace.demo_id, opaque_actor_digest=member_digest,
    ).one().membership_status == "revoked"
    audit_text = " ".join(str(row.reason) for row in db.query(AuditLog).all())
    assert "another-actor" not in audit_text


def test_member_request_rejects_non_digest_and_exposes_management_routes():
    with pytest.raises(ValidationError):
        DemoMemberRequest(bot_id="bot-1", actor_digest="plaintext-actor")
    paths = {route.path for route in router.routes}
    assert "/admin/demo/{demo_id}/members" in paths
    assert "/admin/demo/{demo_id}/members/{actor_digest}" in paths
