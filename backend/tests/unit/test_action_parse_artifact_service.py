from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.services.action_parse_artifact_service import persist_parse_artifact, read_parse_artifact


@compiles(mysql.BIGINT, "sqlite")
def _bigint(_type, _compiler, **_kwargs):
    return "INTEGER"


@compiles(mysql.DATETIME, "sqlite")
def _datetime(_type, _compiler, **_kwargs):
    return "DATETIME"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """CREATE TABLE action_parse_artifact (
                parse_ref VARCHAR(36) PRIMARY KEY, demo_id VARCHAR(64),
                turn_id VARCHAR(36) NOT NULL,
                actor_userid VARCHAR(64) NOT NULL, parse_digest CHAR(64) NOT NULL,
                schema_version VARCHAR(32) NOT NULL, classifier_version VARCHAR(64) NOT NULL,
                session_version INTEGER, payload JSON NOT NULL, expires_at DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(turn_id, parse_digest)
            )"""
        )
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_artifact_round_trip_is_bound_and_reusable(db):
    expires = datetime(2030, 1, 1)
    row = persist_parse_artifact(
        db, parse_ref="parse-1", turn_id="turn-1", actor_userid="u1",
        payload={"intent": "search_job", "structured_data": {"city": "苏州"}},
        parse_digest_value="a" * 64, classifier_version="v1", expires_at=expires,
    )
    db.commit()
    loaded = read_parse_artifact(
        db, "parse-1", turn_id="turn-1", actor_userid="u1",
        parse_digest_value="a" * 64, now=datetime(2029, 1, 1),
    )
    assert loaded is not None and loaded.payload["intent"] == "search_job"
    assert row.parse_ref == loaded.parse_ref


def test_artifact_rejects_pii_and_cross_actor_or_digest(db):
    with pytest.raises(ValueError, match="sensitive"):
        persist_parse_artifact(
            db, parse_ref="parse-pii", turn_id="turn-1", actor_userid="u1",
            payload={"phone": "13800138000"}, parse_digest_value="b" * 64, classifier_version="v1",
        )
    with pytest.raises(ValueError, match="sensitive"):
        persist_parse_artifact(
            db, parse_ref="parse-contact-person", turn_id="turn-1", actor_userid="u1",
            payload={"contact_person": "张经理"}, parse_digest_value="b" * 64, classifier_version="v1",
        )
    persist_parse_artifact(
        db, parse_ref="parse-2", turn_id="turn-1", actor_userid="u1",
        payload={"intent": "show_more"}, parse_digest_value="c" * 64, classifier_version="v1",
    )
    db.commit()
    with pytest.raises(ValueError, match="binding"):
        read_parse_artifact(db, "parse-2", turn_id="turn-1", actor_userid="u2")
    with pytest.raises(ValueError, match="digest"):
        read_parse_artifact(db, "parse-2", turn_id="turn-1", actor_userid="u1", parse_digest_value="d" * 64)


def test_expired_artifact_is_missing_fail_closed(db):
    persist_parse_artifact(
        db, parse_ref="parse-expired", turn_id="turn-1", actor_userid="u1",
        payload={"intent": "search_job"}, parse_digest_value="e" * 64,
        classifier_version="v1", expires_at=datetime(2020, 1, 1),
    )
    db.commit()
    assert read_parse_artifact(
        db, "parse-expired", turn_id="turn-1", actor_userid="u1", now=datetime(2021, 1, 1),
    ) is None
