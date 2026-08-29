import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from scripts import phase11_resume_preflight_media


def test_direct_script_help_does_not_depend_on_pytest_pythonpath():
    backend_dir = Path(__file__).resolve().parents[2]
    script = backend_dir / "scripts" / "phase11_resume_preflight_media.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--batch-size" in result.stdout


def _create_minimal_schema(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE resume (id INTEGER PRIMARY KEY, owner_userid TEXT, images JSON)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE job (id INTEGER PRIMARY KEY, owner_userid TEXT, images JSON)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE media_asset_lifecycle (
                id INTEGER PRIMARY KEY,
                object_key TEXT,
                owner_userid TEXT,
                entity_type TEXT,
                entity_id INTEGER
            )
            """
        )


def _insert_fixture(engine) -> None:
    resume_rows = [
        (
            10101,
            "private-owner-alpha",
            json.dumps([
                "private/resume/alpha.jpg",
                "https://assets.example.test/files/private/resume/alpha.jpg?secret=one",
                "private/shared.jpg",
                "private/missing.jpg",
                "https://evil.example.test/private/leak.jpg",
                778899,
            ]),
        ),
        (
            20202,
            "private-owner-beta",
            json.dumps([
                "private/shared.jpg",
                "private/unbound.jpg",
                "private/owner-mismatch.jpg",
            ]),
        ),
    ]
    job_rows = [
        (
            30303,
            "private-owner-gamma",
            json.dumps(["private/shared.jpg", "private/job-ok.jpg"]),
        ),
        (40404, "private-owner-delta", json.dumps({"not": "an array"})),
    ]
    lifecycle_rows = [
        (1, "private/resume/alpha.jpg", "private-owner-alpha", "resume", 10101),
        (
            2,
            "https://assets.example.test/files/private/resume/alpha.jpg?secret=two",
            "private-owner-alpha",
            "resume",
            10101,
        ),
        (3, "private/shared.jpg", "private-owner-alpha", "resume", 10101),
        (4, "private/unbound.jpg", "private-owner-beta", None, None),
        (5, "private/owner-mismatch.jpg", None, "resume", 20202),
        (6, "private/job-ok.jpg", "private-owner-gamma", "job", 30303),
        (7, "https://evil.example.test/private/lifecycle.jpg", "private-owner-delta", "job", 40404),
    ]
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO resume (id, owner_userid, images) VALUES (?, ?, ?)",
            resume_rows,
        )
        connection.exec_driver_sql(
            "INSERT INTO job (id, owner_userid, images) VALUES (?, ?, ?)",
            job_rows,
        )
        connection.exec_driver_sql(
            """
            INSERT INTO media_asset_lifecycle
                (id, object_key, owner_userid, entity_type, entity_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            lifecycle_rows,
        )


def test_collect_media_preflight_normalizes_and_reports_aggregates_only(
    monkeypatch, capfd,
):
    monkeypatch.setattr(settings, "oss_trusted_origins", "https://assets.example.test")
    monkeypatch.setattr(settings, "oss_local_url_prefix", "/files")
    engine = create_engine("sqlite:///:memory:")
    _create_minimal_schema(engine)
    _insert_fixture(engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    try:
        report = phase11_resume_preflight_media.collect_media_preflight(
            db, batch_size=2
        )
    finally:
        db.close()

    assert report == {
        "resume_row_count": 2,
        "job_row_count": 2,
        "entity_row_count": 4,
        "raw_entity_media_reference_count": 11,
        "normalized_entity_media_reference_count": 9,
        "invalid_images_payload_count": 1,
        "invalid_entity_media_reference_count": 2,
        "valid_entity_canonical_key_count": 6,
        "same_resume_canonical_duplicate_group_count": 1,
        "same_resume_canonical_duplicate_extra_reference_count": 1,
        "cross_entity_shared_canonical_key_count": 1,
        "cross_entity_shared_target_key_count": 3,
        "lifecycle_row_count": 7,
        "normalized_lifecycle_reference_count": 6,
        "invalid_lifecycle_reference_count": 1,
        "valid_lifecycle_canonical_key_count": 5,
        "lifecycle_canonical_collision_key_count": 1,
        "lifecycle_canonical_collision_row_count": 2,
        "missing_lifecycle_canonical_key_count": 1,
        "missing_lifecycle_target_key_count": 1,
        "owner_mismatch_target_key_count": 3,
        "entity_mismatch_target_key_count": 3,
        "binding_mismatch_target_key_count": 4,
        "fully_bound_target_key_count": 3,
        "ready": False,
    }

    monkeypatch.setattr(phase11_resume_preflight_media, "SessionLocal", session_factory)
    monkeypatch.setattr(phase11_resume_preflight_media, "engine", engine)
    monkeypatch.setattr(sys, "argv", ["phase11_resume_preflight_media", "--batch-size", "2"])
    engine.echo = True
    capfd.readouterr()
    assert phase11_resume_preflight_media.main() == 1
    captured = capfd.readouterr()
    serialized = captured.out
    assert json.loads(serialized) == report
    assert captured.err == ""
    assert engine.echo is True
    engine.dispose()

    forbidden_values = (
        "10101",
        "20202",
        "30303",
        "40404",
        "private-owner",
        "assets.example.test",
        "evil.example.test",
        "private/",
        "alpha.jpg",
        "shared.jpg",
    )
    assert all(value not in serialized for value in forbidden_values)


def test_cli_redacts_database_exception_details(monkeypatch, capfd):
    class FailingSessionFactory:
        def __call__(self):
            raise RuntimeError("private SQL parameter 919191")

    monkeypatch.setattr(
        phase11_resume_preflight_media,
        "SessionLocal",
        FailingSessionFactory(),
    )
    monkeypatch.setattr(sys, "argv", ["phase11_resume_preflight_media"])

    assert phase11_resume_preflight_media.main() == 2
    captured = capfd.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": "resume_media_preflight_failed",
        "ready": False,
    }
    assert "919191" not in captured.err


def test_collect_media_preflight_rejects_non_positive_batch_size():
    engine = create_engine("sqlite:///:memory:")
    _create_minimal_schema(engine)
    db = sessionmaker(bind=engine)()
    try:
        with pytest.raises(ValueError, match="^batch_size_must_be_positive$"):
            phase11_resume_preflight_media.collect_media_preflight(db, batch_size=0)
    finally:
        db.close()
        engine.dispose()


def test_collect_media_preflight_is_ready_for_exact_canonical_bindings(monkeypatch):
    monkeypatch.setattr(settings, "oss_trusted_origins", "https://assets.example.test")
    monkeypatch.setattr(settings, "oss_local_url_prefix", "/files")
    engine = create_engine("sqlite:///:memory:")
    _create_minimal_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO resume (id, owner_userid, images) VALUES (?, ?, ?)",
            (50505, "clean-private-owner", json.dumps(["private/clean.jpg"])),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO media_asset_lifecycle
                (id, object_key, owner_userid, entity_type, entity_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "private/clean.jpg", "clean-private-owner", "resume", 50505),
        )
    db = sessionmaker(bind=engine)()
    try:
        report = phase11_resume_preflight_media.collect_media_preflight(db, batch_size=1)
    finally:
        db.close()
        engine.dispose()

    assert report["fully_bound_target_key_count"] == 1
    assert report["binding_mismatch_target_key_count"] == 0
    assert report["ready"] is True


def test_collect_media_preflight_executes_selects_only_and_preserves_rows():
    engine = create_engine("sqlite:///:memory:")
    _create_minimal_schema(engine)
    _insert_fixture(engine)

    table_names = ("resume", "job", "media_asset_lifecycle")
    with engine.connect() as connection:
        before = {
            table_name: connection.exec_driver_sql(
                f"SELECT * FROM {table_name} ORDER BY id"
            ).all()
            for table_name in table_names
        }

    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    db = sessionmaker(bind=engine)()
    try:
        phase11_resume_preflight_media.collect_media_preflight(db, batch_size=1)
    finally:
        db.close()

    with engine.connect() as connection:
        after = {
            table_name: connection.exec_driver_sql(
                f"SELECT * FROM {table_name} ORDER BY id"
            ).all()
            for table_name in table_names
        }
    event.remove(engine, "before_cursor_execute", record_statement)
    engine.dispose()

    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert after == before
