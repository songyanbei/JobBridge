"""Phase 11 stage-1 acceptance contract.

These tests deliberately exercise release metadata and migration artefacts as
public contracts.  They do not execute a migration against a developer
database; the real-MySQL gate covers execution separately.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects import mysql

from app.config import Settings
from app.models import (
    Phase11MigrationLedger,
    Phase11ResumeLifecycleBackup,
    Phase11ResumeMediaKeyScan,
    Resume,
    ResumeMediaIsolationIssue,
    ResumeReplacement,
    ResumeReplacementRolloutAssignment,
)
from app.schemas.resume import ResumeCreate
from scripts import apply_phase11_migrations as runner


BACKEND = Path(__file__).resolve().parents[2]
MANIFEST = BACKEND / "sql" / "migrations" / "phase11_manifest.json"


PHASE11_ADDITIVE_MODELS = {
    model.__tablename__: model
    for model in (
        ResumeReplacement,
        ResumeReplacementRolloutAssignment,
        Phase11MigrationLedger,
        ResumeMediaIsolationIssue,
        Phase11ResumeMediaKeyScan,
        Phase11ResumeLifecycleBackup,
    )
}


class _CheckpointResult:
    def __init__(self, cursor, summary):
        self._row = {
            "resume_cursor_json": cursor,
            "verification_digest": runner._canonical_digest(summary),
        }

    def mappings(self):
        return self

    def one(self):
        return self._row


def _create_table_body(sql: str, table_name: str) -> str:
    match = re.search(
        rf"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+`{re.escape(table_name)}`\s*\(",
        sql,
        re.IGNORECASE,
    )
    assert match, table_name
    start = match.end()
    depth = 1
    quote = None
    for offset, char in enumerate(sql[start:], start=start):
        if quote:
            if char == quote and sql[offset - 1] != "\\":
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[start:offset]
    raise AssertionError(f"unterminated CREATE TABLE for {table_name}")


def _top_level_definitions(body: str) -> list[str]:
    definitions: list[str] = []
    start = 0
    depth = 0
    quote = None
    for index, char in enumerate(body):
        if quote:
            if char == quote and body[index - 1] != "\\":
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            definitions.append(body[start:index].strip())
            start = index + 1
    definitions.append(body[start:].strip())
    return definitions


def _normalize_type(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower()).replace("integer unsigned", "int unsigned")


def _normalize_default(value: str | None) -> str | None:
    if value is None or value.upper() == "NULL":
        return None
    return value.strip("'\"").lower()


def _ddl_contract(sql: str, table_name: str) -> dict:
    columns = {}
    primary_key = ()
    indexes = {}
    for definition in _top_level_definitions(_create_table_body(sql, table_name)):
        column_match = re.match(r"`([^`]+)`\s+(.+)", definition, re.DOTALL)
        if column_match:
            name, remainder = column_match.groups()
            boundary = re.search(
                r"\s+(?=NOT\s+NULL|NULL(?:\s|$)|DEFAULT\s|AUTO_INCREMENT(?:\s|$)|COMMENT\s|ON\s+UPDATE)",
                remainder,
                re.IGNORECASE,
            )
            sql_type = remainder[: boundary.start()] if boundary else remainder
            default_match = re.search(
                r"\bDEFAULT\s+((?:CURRENT_TIMESTAMP)(?:\(\d+\))?|NULL|'[^']*'|\d+)",
                remainder,
                re.IGNORECASE,
            )
            columns[name] = (
                _normalize_type(sql_type),
                not bool(re.search(r"\bNOT\s+NULL\b", remainder, re.IGNORECASE)),
                _normalize_default(default_match.group(1) if default_match else None),
                bool(re.search(r"\bAUTO_INCREMENT\b", remainder, re.IGNORECASE)),
                bool(re.search(r"\bON\s+UPDATE\s+CURRENT_TIMESTAMP(?:\(\d+\))?", remainder, re.IGNORECASE)),
            )
            continue
        primary_match = re.match(r"PRIMARY\s+KEY\s*\((.+)\)", definition, re.IGNORECASE)
        if primary_match:
            primary_key = tuple(re.findall(r"`([^`]+)`", primary_match.group(1)))
            continue
        index_match = re.match(
            r"(UNIQUE\s+)?KEY\s+`([^`]+)`\s*\((.+)\)", definition, re.IGNORECASE
        )
        if index_match:
            indexes[index_match.group(2)] = (
                bool(index_match.group(1)),
                tuple(re.findall(r"`([^`]+)`", index_match.group(3))),
            )
    return {"columns": columns, "primary_key": primary_key, "indexes": indexes}


def _orm_contract(model) -> dict:
    table = model.__table__
    dialect = mysql.dialect()
    columns = {}
    for column in table.columns:
        default = None
        if column.server_default is not None:
            argument = column.server_default.arg
            default = str(argument.compile(dialect=dialect) if hasattr(argument, "compile") else argument)
            match = re.match(r"(CURRENT_TIMESTAMP(?:\(\d+\))?)", default, re.IGNORECASE)
            if match:
                default = match.group(1)
        columns[column.name] = (
            _normalize_type(column.type.compile(dialect=dialect)),
            column.nullable,
            _normalize_default(default),
            bool(column.autoincrement is True),
            column.server_onupdate is not None,
        )
    indexes = {
        index.name: (index.unique, tuple(column.name for column in index.columns))
        for index in table.indexes
    }
    indexes.update(
        {
            constraint.name: (True, tuple(column.name for column in constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
    )
    return {
        "columns": columns,
        "primary_key": tuple(column.name for column in table.primary_key.columns),
        "indexes": indexes,
    }


def test_candidate_ttl_range_is_frozen_to_one_through_365_days():
    assert Settings(ttl_resume_candidate_days=1).ttl_resume_candidate_days == 1
    assert Settings(ttl_resume_candidate_days=365).ttl_resume_candidate_days == 365
    with pytest.raises(ValidationError):
        Settings(ttl_resume_candidate_days=0)
    with pytest.raises(ValidationError):
        Settings(ttl_resume_candidate_days=366)


def test_five_fail_closed_resume_switches_are_independently_addressable():
    expected = {
        "resume_lifecycle_v2_enabled",
        "resume_replacement_enabled",
        "resume_expiry_cleanup_enabled",
        "resume_candidate_cleanup_enabled",
        "resume_hard_delete_enabled",
    }
    fields = Settings.model_fields
    assert expected <= fields.keys()
    settings = Settings()
    assert all(getattr(settings, name) is False for name in expected)


def test_manifest_pins_checksums_and_ready_minimum_build_anchor():
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    anchor = document["minimum_build"]
    assert anchor == {
        "ready": True,
        "build_number": 249,
        "build_sha": "083be7e8fa37045a94f5247ea4fb9cb8d1a35652",
        "capabilities": [
            "resume_nullable_dto",
            "resume_lifecycle_double_write",
        ],
    }
    for entry in document["steps"]:
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert entry["stage"] in {"pre_cutover", "post_cutover", "verify", "down"}
        assert runner.file_sha256(BACKEND / entry["path"]) == entry["sha256"]
    pre_cutover = [entry for entry in document["steps"] if entry["stage"] == "pre_cutover"]
    assert [(entry["kind"], entry["path"]) for entry in pre_cutover] == [
        ("sql", "sql/migrations/phase11_001_resume_lifecycle_additive.sql"),
        ("sql", "sql/migrations/phase11_002_resume_config_seed.sql"),
    ]


def test_all_three_resumable_reconciliation_tools_exist():
    expected = {
        "phase11_resume_media_reconcile.py",
        "phase11_resume_orphan_target_reconcile.py",
        "phase11_resume_deleted_target_backfill.py",
    }
    present = {path.name for path in (BACKEND / "scripts").glob("phase11_resume_*.py")}
    assert expected <= present


def test_canonical_schema_contains_all_additive_phase11_structures():
    schema = (BACKEND / "sql" / "schema.sql").read_text(encoding="utf-8")
    for table in {
        "resume_replacement",
        "resume_replacement_rollout_assignment",
        "phase11_migration_ledger",
        "resume_media_isolation_issue",
        "phase11_resume_lifecycle_backup",
    }:
        assert f"CREATE TABLE `{table}`" in schema


def test_phase11_additive_orm_matches_canonical_and_migration_table_shapes():
    """Keep every stage-1 ORM table aligned with both DDL sources.

    The contract covers column names/types/nullability/defaults/autoincrement,
    ordered primary keys, named unique constraints, and secondary indexes.
    Foreign keys are intentionally absent from all six structures.
    """
    canonical = (BACKEND / "sql" / "schema.sql").read_text(encoding="utf-8")
    additive = (
        BACKEND / "sql" / "migrations" / "phase11_001_resume_lifecycle_additive.sql"
    ).read_text(encoding="utf-8")

    for table_name, model in PHASE11_ADDITIVE_MODELS.items():
        canonical_contract = _ddl_contract(canonical, table_name)
        migration_contract = _ddl_contract(additive, table_name)
        assert migration_contract == canonical_contract, table_name
        assert _orm_contract(model) == canonical_contract, table_name
        assert not list(model.__table__.foreign_keys), table_name


def test_every_phase11_auto_updated_orm_column_is_server_managed():
    models = (Resume, ResumeReplacement, Phase11MigrationLedger, ResumeMediaIsolationIssue)
    for model in models:
        column = model.__table__.c.updated_at
        assert column.server_onupdate is not None, model.__tablename__
        assert column.onupdate is None, model.__tablename__


def test_media_scan_registry_shape_is_hash_only_and_restart_safe():
    contract = _orm_contract(Phase11ResumeMediaKeyScan)
    assert tuple(contract["columns"]) == (
        "resume_id",
        "key_hash",
        "reference_kind",
        "reference_count",
        "first_seen_at",
    )
    assert contract["primary_key"] == ("resume_id", "key_hash", "reference_kind")
    assert contract["indexes"] == {
        "idx_phase11_media_scan_key": (
            False,
            ("key_hash", "reference_kind", "resume_id"),
        )
    }
    assert "first_resume_id" not in contract["columns"]


def test_resume_lifecycle_columns_use_microsecond_precision_in_canonical_schema():
    schema = (BACKEND / "sql" / "schema.sql").read_text(encoding="utf-8")
    resume_ddl = schema[schema.index("CREATE TABLE `resume`") :]
    resume_ddl = resume_ddl[: resume_ddl.index(";", resume_ddl.index("ENGINE=InnoDB"))]
    for column in {
        "created_at",
        "updated_at",
        "activated_at",
        "candidate_expires_at",
        "expires_at",
        "deleted_at",
    }:
        assert re.search(rf"`{column}`\s+DATETIME\(6\)", resume_ddl), column


def test_down_migration_has_explicit_fail_closed_guards():
    sql = (BACKEND / "sql" / "migrations" / "phase11_down_001_resume_lifecycle.sql").read_text(
        encoding="utf-8"
    )
    assert "CHECK (`ok`=1)" in sql
    assert "phase11_down_guard" in sql
    assert "phase11_export_guard" in sql
    assert "phase11_null_ttl_guard" in sql
    assert "resume_replacement" in sql
    assert "resume_media_isolation_issue" in sql
    assert "phase11_migration_ledger" in sql
    assert "r.deleted_at=COALESCE" not in sql
    assert "DROP TABLE `resume_replacement`" in sql
    assert "phase11_resume_down_replacement_backup" in sql
    assert "phase11_resume_down_assignment_backup" in sql
    assert "phase11_resume_down_media_issue_backup" in sql
    assert "phase11_resume_down_ledger_backup" in sql
    assert "phase11_resume_down_export_audit" in sql
    assert "phase11_capture_down_export_audits" in sql
    assert "BIT_XOR" not in sql
    assert "CRC32" not in sql
    assert "REPLACE INTO `phase11_resume_down_backup`" in sql


def test_orphan_reconcile_requires_explicit_isolated_redis_target():
    source = (BACKEND / "scripts" / "phase11_resume_orphan_target_reconcile.py").read_text(
        encoding="utf-8"
    )
    assert 'add_argument("--redis-dsn",required=True)' in source
    assert 'add_argument("--redis-namespace",required=True)' in source
    assert "get_redis(" not in source


def test_runner_passes_explicit_redis_target_only_to_orphan_step(monkeypatch):
    called = {}

    class Completed:
        returncode = 0
        stdout = ('{"status":"succeeded","orphan_cursor":{"last_target_id":0,'
                  '"audit_summary":{"found":0,"created":0,"last_target_id":0}},'
                  '"audit_summary":{"found":0,"created":0,"last_target_id":0}}')

    def fake_run(command, **kwargs):
        called["command"] = command
        return Completed()

    class Begin:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def execute(self, *_args, **_kwargs):
            summary = {"found": 0, "created": 0, "last_target_id": 0}
            return _CheckpointResult(
                {"last_target_id": 0, "audit_summary": summary}, summary,
            )

    class Engine:
        def begin(self):
            return Begin()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner._run_python(
        Engine(), "mysql://isolated", {
            "file": BACKEND / "scripts" / "phase11_resume_orphan_target_reconcile.py",
            "key": "phase11_resume_orphan_target_reconcile",
            "cursor_key": "orphan_cursor",
        }, {}, redis_dsn="redis://127.0.0.1:6399/15", redis_namespace="phase11-test",
    )
    assert called["command"][-4:] == [
        "--redis-dsn", "redis://127.0.0.1:6399/15",
        "--redis-namespace", "phase11-test",
    ]


@pytest.mark.parametrize(
    ("ledger_value", "expected"),
    [
        ({"last_target_id": 41}, {"last_target_id": 41}),
        ('{"last_target_id":41}', {"last_target_id": 41}),
        (None, {}),
    ],
)
def test_runner_normalizes_ledger_cursor_without_double_encoding(
    monkeypatch, ledger_value, expected
):
    calls = []
    writes = []

    class Completed:
        returncode = 0
        stdout = ('{"status":"succeeded","media_cursor":{"last_target_id":42,'
                  '"audit_summary":{"scanned":1,"last_target_id":42}},'
                  '"audit_summary":{"scanned":1,"last_target_id":42}}')

    def fake_run(command, **_kwargs):
        calls.append(command)
        return Completed()

    class Begin:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def execute(self, _statement, parameters):
            writes.append(parameters)
            summary = {"scanned": 1, "last_target_id": 42}
            return _CheckpointResult(
                {"last_target_id": 42, "audit_summary": summary}, summary,
            )

    class Engine:
        def begin(self):
            return Begin()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner._run_python(
        Engine(), "mysql://isolated", {
            "file": BACKEND / "scripts" / "phase11_resume_media_reconcile.py",
            "key": "phase11_resume_media_reconcile",
            "cursor_key": "media_cursor",
        }, ledger_value, redis_dsn=None, redis_namespace=None,
    )

    cursor_argument = calls[0][calls[0].index("--resume-cursor-json") + 1]
    assert json.loads(cursor_argument) == expected
    assert not isinstance(json.loads(cursor_argument), str)
    assert writes[0]["key"] == "phase11_resume_media_reconcile"


@pytest.mark.parametrize("bad_cursor", ["not-json", "[]", [], 7, '"object-text"'])
def test_runner_fails_closed_for_malformed_or_non_object_ledger_cursor(
    monkeypatch, bad_cursor
):
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("invalid cursor must not launch subprocess"),
    )
    with pytest.raises(
        RuntimeError, match="ledger_resume_cursor_(invalid_json|must_be_object)"
    ):
        runner._run_python(
            object(), "mysql://isolated", {
                "file": BACKEND / "scripts" / "phase11_resume_media_reconcile.py",
                "key": "phase11_resume_media_reconcile",
                "cursor_key": "media_cursor",
            }, bad_cursor, redis_dsn=None, redis_namespace=None,
        )


def test_runner_resume_after_python_crash_reuses_string_ledger_cursor(monkeypatch):
    commands = []
    writes = []

    class Completed:
        returncode = 0
        stdout = ('{"status":"succeeded","media_cursor":{"last_target_id":12,'
                  '"audit_summary":{"scanned":1,"last_target_id":12}},'
                  '"audit_summary":{"scanned":1,"last_target_id":12}}')

    def fake_run(command, **_kwargs):
        commands.append(command)
        if len(commands) == 1:
            raise OSError("simulated process crash")
        return Completed()

    class Begin:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def execute(self, _statement, parameters):
            writes.append(parameters)
            summary = {"scanned": 1, "last_target_id": 12}
            return _CheckpointResult(
                {"last_target_id": 12, "audit_summary": summary}, summary,
            )

    class Engine:
        def begin(self):
            return Begin()

    step = {
        "file": BACKEND / "scripts" / "phase11_resume_media_reconcile.py",
        "key": "phase11_resume_media_reconcile",
        "cursor_key": "media_cursor",
    }
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(OSError, match="simulated process crash"):
        runner._run_python(
            Engine(), "mysql://isolated", step, '{"last_target_id":11}',
            redis_dsn=None, redis_namespace=None,
        )
    assert writes == []

    runner._run_python(
        Engine(), "mysql://isolated", step, '{"last_target_id":11}',
        redis_dsn=None, redis_namespace=None,
    )
    for command in commands:
        cursor_argument = command[command.index("--resume-cursor-json") + 1]
        assert json.loads(cursor_argument) == {"last_target_id": 11}
    assert writes[0]["key"] == "phase11_resume_media_reconcile"


def test_python_step_requires_structured_audit_summary(monkeypatch):
    class Completed:
        returncode = 0
        stdout = '{"status":"succeeded","media_cursor":{"last_target_id":1}}'

    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: Completed())
    with pytest.raises(RuntimeError, match="python_step_audit_summary_missing"):
        runner._run_python(
            object(), "mysql://isolated", {
                "file": BACKEND / "scripts" / "phase11_resume_media_reconcile.py",
                "key": "phase11_resume_media_reconcile",
                "cursor_key": "media_cursor",
            }, {}, redis_dsn=None, redis_namespace=None,
        )


def test_down_guards_never_depend_on_cross_process_session_variables():
    sql = (BACKEND / "sql" / "migrations" / "phase11_down_001_resume_lifecycle.sql").read_text(
        encoding="utf-8"
    )
    assert "@phase11_down_blockers" not in sql
    assert "@phase11_export_blockers" not in sql
    assert "@phase11_null_ttl_count" not in sql


def test_runner_rejects_duplicate_ddl_without_shape_proof():
    class Connect:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    class Engine:
        def connect(self):
            return Connect()

    with pytest.raises(RuntimeError, match="not_provably_equivalent"):
        runner._validate_duplicate_ddl_shape(Engine(), "CREATE TABLE already_wrong (id INT)")


def test_media_reconcile_has_durable_hash_only_shared_key_registry():
    source = (BACKEND / "scripts" / "phase11_resume_media_reconcile.py").read_text(
        encoding="utf-8"
    )
    assert "phase11_resume_media_key_scan" in source
    assert "key_hash" in source
    assert "first_resume_id" in source
    assert "for affected_id in (first_id, int(scan_row[\"id\"]))" in source


def test_stage1_create_contract_still_requires_legacy_expiry():
    with pytest.raises(ValidationError) as exc:
        ResumeCreate(
            owner_userid="worker-1", expected_cities=["上海"],
            expected_job_categories=["普工"], salary_expect_floor_monthly=5000,
            gender="男", age=30, raw_text="完整简历",
        )
    assert "expires_at" in str(exc.value)


def test_build_anchor_does_not_require_instance_sha_to_equal_anchor(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
    response = Response()
    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(runner.json, "load", lambda _response: {
        "build_number": 12, "build_sha": "1" * 40, "capabilities": ["nullable"],
    })
    digest = runner._probe_builds({
        "ready": True, "build_number": 10, "build_sha": "0" * 40,
        "capabilities": ["nullable"],
    }, ["http://instance/health=" + "1" * 40])
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_orphan_reconcile_and_verify_cover_same_persisted_fact_families():
    reconcile = (BACKEND / "scripts" / "phase11_resume_orphan_target_reconcile.py").read_text(encoding="utf-8")
    verify = (BACKEND / "sql" / "migrations" / "phase11_resume_verify.sql").read_text(encoding="utf-8")
    for source in {
        "recommendation_delivery", "conversation_log", "wecom_outbound_outbox",
        "recommendation_exposure_daily", "event_log", "recommendation_impression",
        "recommendation_request", "recommendation_search_attempt",
    }:
        assert source in reconcile
        assert source in verify


def test_allowlist_rejects_unknown_duplicate_and_blank_members():
    from app.services.resume_replacement_rollout_service import validate_allowlist
    with pytest.raises(ValueError):
        validate_allowlist({"revision": 1, "userids": ["worker", "worker"]})
    with pytest.raises(ValueError):
        validate_allowlist({"revision": 1, "userids": ["   "]})
    with pytest.raises(ValueError):
        validate_allowlist({"revision": 1, "userids": [], "extra": True})


@pytest.mark.parametrize("revision", [-1, 0, 1.5, "1", 18_446_744_073_709_551_616])
def test_allowlist_revision_matches_unsigned_bigint_database_contract(revision):
    from app.services.resume_replacement_rollout_service import validate_allowlist

    with pytest.raises(ValueError):
        validate_allowlist({"revision": revision, "userids": []})


@pytest.mark.parametrize("revision", [1, 18_446_744_073_709_551_615])
def test_allowlist_revision_accepts_contract_boundaries(revision):
    from app.services.resume_replacement_rollout_service import validate_allowlist

    assert validate_allowlist({"revision": revision, "userids": []}).revision == revision


def test_down_replay_only_accepts_missing_destructive_objects():
    class Orig:
        args = (1051, "unknown table")
    class Error:
        orig = Orig()
    assert runner._already_applied_destructive_ddl(Error(), "DROP TABLE `resume_replacement`")
    assert not runner._already_applied_destructive_ddl(Error(), "DELETE FROM resume")


def test_runner_never_includes_child_stderr_in_failure(monkeypatch):
    canaries = "object_key=SECRET userid=WORKER id=991827"

    class Completed:
        returncode = 2
        stdout = canaries
        stderr = canaries

    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: Completed())
    with pytest.raises(RuntimeError) as raised:
        runner._run_python(
            object(), "mysql://user:password@invalid/database", {
                "file": BACKEND / "scripts" / "phase11_resume_media_reconcile.py",
                "key": "phase11_resume_media_reconcile",
                "cursor_key": "media_cursor",
            }, {}, redis_dsn=None, redis_namespace=None,
        )
    assert str(raised.value) == "python_step_failed"
    assert all(token not in str(raised.value) for token in canaries.split())


@pytest.mark.parametrize(
    ("script", "extra_args", "error_code"),
    [
        ("phase11_resume_media_reconcile.py", [], "phase11_media_reconcile_failed"),
        ("phase11_resume_lifecycle_backfill.py", [], "phase11_lifecycle_backfill_failed"),
        ("phase11_resume_deleted_target_backfill.py", [], "phase11_deleted_target_backfill_failed"),
        ("phase11_resume_config_seed.py", [], "phase11_config_seed_failed"),
        (
            "phase11_resume_orphan_target_reconcile.py",
            ["--redis-dsn", "redis://127.0.0.1:1/0", "--redis-namespace", "safe-test"],
            "phase11_orphan_reconcile_failed",
        ),
    ],
)
def test_phase11_tool_cli_failures_are_stable_and_redacted(
    script, extra_args, error_code,
):
    canaries = ["OBJECT_KEY_CANARY", "USERID_CANARY", "ID_CANARY_991827"]
    dsn = "mysql+pymysql://user:OBJECT_KEY_CANARY@127.0.0.1:1/USERID_CANARY"
    completed = subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / script), "--dsn", dsn,
         "--resume-cursor-json", '{"id":"ID_CANARY_991827"}', *extra_args],
        cwd=BACKEND, capture_output=True, text=True, timeout=10, check=False,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert json.loads(completed.stderr) == {"status": "failed", "error_code": error_code}
    assert all(canary not in combined for canary in canaries)


def test_phase11_database_engines_hide_sql_parameters():
    scripts = [
        "apply_phase11_migrations.py", "phase11_resume_media_reconcile.py",
        "phase11_resume_lifecycle_backfill.py",
        "phase11_resume_deleted_target_backfill.py",
        "phase11_resume_orphan_target_reconcile.py", "phase11_resume_config_seed.py",
    ]
    for script in scripts:
        source = (BACKEND / "scripts" / script).read_text(encoding="utf-8")
        assert "hide_parameters=True" in source, script


def test_phase11_runner_cli_failure_is_stable_and_redacted():
    canaries = ["OBJECT_KEY_CANARY", "USERID_CANARY", "ID_CANARY_991827"]
    dsn = "mysql+pymysql://USERID_CANARY:OBJECT_KEY_CANARY@127.0.0.1:1/ID_CANARY_991827"
    completed = subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "apply_phase11_migrations.py"),
         "check", "--dsn", dsn],
        cwd=BACKEND, capture_output=True, text=True, timeout=10, check=False,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert json.loads(completed.stderr) == {
        "status": "failed", "error_code": "phase11_migration_runner_failed",
    }
    assert all(canary not in combined for canary in canaries)


@pytest.mark.parametrize(
    "raw", ["01", "+1", " 1", "1 ", "", "\uff11", "١", "1\n"],
)
@pytest.mark.parametrize("key", ["ttl.resume.days", "ttl.resume.candidate.days"])
def test_admin_resume_ttl_update_rejects_noncanonical_ascii_decimal(
    monkeypatch, key, raw,
):
    from app.core.exceptions import BusinessException
    from app.services import system_config_service as service

    item = SimpleNamespace(
        config_key=key, config_value="7", value_type="int", updated_by=None,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = item
    monkeypatch.setattr(service, "write_admin_log", lambda *_args, **_kwargs: None)
    with pytest.raises(BusinessException):
        service.update(db, key, raw, None, "phase11-test")
    assert item.config_value == "7"
    db.commit.assert_not_called()


def test_legacy_non_resume_integer_config_keeps_compatible_parser(monkeypatch):
    from app.services import system_config_service as service

    item = SimpleNamespace(
        config_key="ttl.job.days", config_value="7", value_type="int", updated_by=None,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = item
    monkeypatch.setattr(service, "write_admin_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "invalidate_config_cache", lambda *_args: None)
    service.update(db, "ttl.job.days", "01", None, "phase11-test")
    assert item.config_value == "01"
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    ("getter_name", "raw", "expected"),
    [
        ("get_resume_ttl_days", "1", 1),
        ("get_resume_ttl_days", "3650", 3650),
        ("get_resume_candidate_ttl_days", "1", 1),
        ("get_resume_candidate_ttl_days", "365", 365),
        ("get_resume_ttl_days", "01", 30),
        ("get_resume_ttl_days", "+1", 30),
        ("get_resume_ttl_days", " 1", 30),
        ("get_resume_ttl_days", "1 ", 30),
        ("get_resume_ttl_days", "", 30),
        ("get_resume_ttl_days", "\uff11", 30),
        ("get_resume_ttl_days", "١", 30),
        ("get_resume_ttl_days", "0", 30),
        ("get_resume_ttl_days", "3651", 30),
        ("get_resume_candidate_ttl_days", "01", 7),
        ("get_resume_candidate_ttl_days", "+1", 7),
        ("get_resume_candidate_ttl_days", "\uff11", 7),
        ("get_resume_candidate_ttl_days", "366", 7),
    ],
)
def test_resume_ttl_reader_uses_canonical_ascii_and_safe_defaults(
    getter_name, raw, expected,
):
    from app.services import lifecycle_config_service as service

    row = SimpleNamespace(config_value=raw)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    assert getattr(service, getter_name)(db) == expected


def test_legacy_job_ttl_reader_keeps_compatible_integer_parser():
    from app.services.lifecycle_config_service import get_job_ttl_days

    row = SimpleNamespace(config_value="01")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    assert get_job_ttl_days(db) == 1


@pytest.mark.parametrize("raw", ["01", "+1", " 1", "1 ", "", "\uff11", "١"])
@pytest.mark.parametrize(
    ("key", "error"),
    [
        ("ttl.resume.days", "invalid_existing_resume_ttl"),
        ("ttl.resume.candidate.days", "invalid_existing_candidate_ttl"),
    ],
)
def test_config_seed_parser_rejects_noncanonical_ascii_decimal(key, error, raw):
    from scripts.phase11_resume_config_seed import _validate

    with pytest.raises(RuntimeError, match=error):
        _validate(key, raw, "int")


def test_config_seed_fails_closed_on_valid_legacy_rollout_payload():
    from scripts.phase11_resume_config_seed import _validate

    with pytest.raises(RuntimeError, match="legacy_rollout_config_present"):
        _validate(
            "rollout.resume_replacement.allowlist",
            '{"revision":1,"userids":[]}',
            "json",
        )
