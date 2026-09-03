import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import DemoPrincipal, DemoResource, DemoWorkspace, DemoWorkspaceMember, User
from app.services import demo_mode_service
from app.services import demo_workspace_admin_service as admin_service


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
