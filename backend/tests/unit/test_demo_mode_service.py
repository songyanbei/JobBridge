from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import DemoPrincipal, DemoResource, DemoWorkspace, DemoWorkspaceMember, User
from app.services import demo_mode_service as service


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    # User uses MySQL-only unsigned types in production; this contract test
    # needs only the subset used when provisioning synthetic principals.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE user ("
            "external_userid VARCHAR(64) PRIMARY KEY, role VARCHAR(16) NOT NULL, "
            "display_name VARCHAR(64), company VARCHAR(128), address VARCHAR(255), "
            "contact_person VARCHAR(64), phone VARCHAR(32), phone_ciphertext BLOB, "
            "phone_key_version INTEGER, phone_digest VARCHAR(64), contact_person_ciphertext BLOB, "
            "contact_person_key_version INTEGER, contact_person_digest VARCHAR(64), "
            "wechat_ciphertext BLOB, wechat_key_version INTEGER, wechat_digest VARCHAR(64), "
            "can_search_jobs INTEGER NOT NULL DEFAULT 0, can_search_workers INTEGER NOT NULL DEFAULT 0, "
            "status VARCHAR(16) NOT NULL DEFAULT 'active', blocked_reason VARCHAR(255), "
            "registered_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_active_at DATETIME, extra JSON)"
        )
    tables = [
        DemoWorkspace.__table__, DemoWorkspaceMember.__table__,
        DemoPrincipal.__table__, DemoResource.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def demo_settings(monkeypatch):
    actor = service.actor_digest_for_lookup("actor-one")
    monkeypatch.setattr(service.settings, "app_env", "test")
    monkeypatch.setattr(service.settings, "demo_mode_enabled", True)
    monkeypatch.setattr(service.settings, "demo_allowed_bot_ids", "bot-1")
    monkeypatch.setattr(service.settings, "demo_allowed_actor_digests", actor)
    monkeypatch.setattr(service.settings, "demo_max_active_workspaces", 2)
    return actor


def test_settings_reject_demo_mode_outside_non_production(monkeypatch):
    with pytest.raises(ValidationError, match="DEMO_MODE_ENABLED"):
        Settings(
            _env_file=None,
            app_env="production",
            demo_mode_enabled=True,
            demo_allowed_bot_ids="bot-1",
            cors_origins="https://admin.example.com",
        )


def test_settings_require_bot_allowlist_when_enabled():
    with pytest.raises(ValidationError, match="DEMO_ALLOWED_BOT_IDS"):
        Settings(_env_file=None, app_env="test", demo_mode_enabled=True)


def test_workspace_creates_three_synthetic_principals_without_mutating_real_role(db, demo_settings):
    real = User(external_userid="real-user", role="worker", can_search_jobs=1, can_search_workers=0)
    db.add(real)
    db.flush()

    workspace = service.create_workspace(
        db,
        name="联调演示",
        bot_id="bot-1",
        actor_digest_value=demo_settings,
        canonical_actor_userid="real-user",
        created_by="admin",
    )
    db.commit()

    assert db.query(DemoPrincipal).filter(DemoPrincipal.demo_id == workspace.demo_id).count() == 3
    assert {p.role for p in db.query(DemoPrincipal).all()} == {"worker", "factory", "broker"}
    assert db.query(User).filter(User.external_userid == "real-user").one().role == "worker"
    assert db.query(User).filter(User.external_userid.like("demo_%")).count() == 3


def test_authorize_switch_and_revoke_use_bot_digest_membership(db, demo_settings):
    workspace = service.create_workspace(
        db, name="联调演示", bot_id="bot-1", actor_digest_value=demo_settings,
        canonical_actor_userid="actor-one-user", created_by="admin",
    )
    second_digest = service.actor_digest_for_lookup("actor-two")
    member = service.authorize_member(
        db, demo_id=workspace.demo_id, bot_id="bot-1", actor_digest_value=second_digest,
        canonical_actor_userid="actor-two-user", granted_by="admin",
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10),
    )
    assert member.membership_status == "active"

    context = service.switch_role(
        db, demo_id=workspace.demo_id, bot_id="bot-1", actor_digest_value=second_digest,
        active_role="factory",
    )
    assert context.demo_mode is True
    assert context.real_actor_userid == "actor-two-user"
    assert context.reply_userid == "actor-two-user"
    assert context.active_role == "factory"
    assert context.effective_userid.startswith("demo_factory_")
    assert db.query(User).filter(User.external_userid == "actor-two-user").count() == 0

    service.revoke_member(db, demo_id=workspace.demo_id, bot_id="bot-1", actor_digest_value=second_digest)
    with pytest.raises(service.DemoAuthorizationError, match="active workspace member"):
        service.switch_role(
            db, demo_id=workspace.demo_id, bot_id="bot-1", actor_digest_value=second_digest,
            active_role="broker",
        )


def test_expired_membership_is_closed_and_cannot_switch(db, demo_settings):
    workspace = service.create_workspace(
        db, name="联调演示", bot_id="bot-1", actor_digest_value=demo_settings,
        created_by="admin",
    )
    second_digest = service.actor_digest_for_lookup("actor-two")
    service.authorize_member(
        db, demo_id=workspace.demo_id, bot_id="bot-1", actor_digest_value=second_digest,
        granted_by="admin", expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
    )
    with pytest.raises(service.DemoAuthorizationError):
        service.switch_role(
            db, demo_id=workspace.demo_id, bot_id="bot-1", actor_digest_value=second_digest,
            active_role="worker",
        )
    assert db.query(DemoWorkspaceMember).filter(
        DemoWorkspaceMember.opaque_actor_digest == second_digest,
    ).one().membership_status == "expired"


def test_resource_registration_is_idempotent_and_workspace_disable_blocks_it(db, demo_settings):
    workspace = service.create_workspace(
        db, name="联调演示", bot_id="bot-1", actor_digest_value=demo_settings, created_by="admin",
    )
    first = service.register_resource(
        db, demo_id=workspace.demo_id, resource_type="job", target_id="1001", metadata={"x": 1},
    )
    second = service.register_resource(
        db, demo_id=workspace.demo_id, resource_type="job", target_id="1001", metadata={"x": 2},
    )
    assert first.resource_id == second.resource_id
    assert second.metadata_json == {"x": 2}
    workspace.status = "disabled"
    with pytest.raises(service.DemoWorkspaceStateError):
        service.register_resource(db, demo_id=workspace.demo_id, resource_type="resume", target_id="2")


def test_demo_gate_fails_closed_for_unallowlisted_bot(db, demo_settings):
    assert service.demo_request_allowed(
        db, bot_id="other-bot", actor_digest_value=demo_settings,
    ) is False


def test_model_and_migration_contracts_are_additive():
    from pathlib import Path

    migration = (Path(__file__).parents[2] / "sql" / "migrations" / "phase17_001_demo_control_plane.sql").read_text(encoding="utf-8").lower()
    assert all(name in migration for name in ("demo_workspace", "demo_workspace_member", "demo_principal", "demo_resource"))
    assert "alter table user" not in migration
    assert "delete from" not in migration
    assert "drop table" not in migration
    assert DemoPrincipal.__table__.constraints


def test_message_provider_contract_keeps_real_actor_as_reply_target(db, demo_settings):
    from app.services import demo_workspace_service as provider

    workspace = service.create_workspace(
        db, name="联调演示", bot_id="bot-1", actor_digest_value=demo_settings,
        canonical_actor_userid=None, created_by="admin",
    )
    context = provider.activate_for_actor(db, "actor-one", "bot-1", "broker")
    assert context is not None
    assert context.real_actor_userid == "actor-one"
    assert context.reply_userid == "actor-one"
    assert context.effective_userid.startswith("demo_broker_")
    assert context.demo_id == workspace.demo_id
