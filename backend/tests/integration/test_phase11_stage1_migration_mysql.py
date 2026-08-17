"""Repeatable Phase 11 stage-1 gates for an explicitly isolated MySQL/Redis."""
from __future__ import annotations

import os
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError

from app.core.redis_client import validate_redis_durability_policy
from app.models import (
    Phase11MigrationLedger,
    Phase11ResumeLifecycleBackup,
    Phase11ResumeMediaKeyScan,
    Resume,
    ResumeMediaIsolationIssue,
    ResumeReplacement,
    ResumeReplacementRolloutAssignment,
    User,
)
from scripts import apply_phase11_migrations as runner
from scripts.phase11_resume_config_seed import seed
from scripts.phase11_resume_lifecycle_backfill import backfill
from scripts import phase11_resume_lifecycle_backfill as lifecycle_backfill
from scripts.phase11_resume_media_reconcile import reconcile

BACKEND = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


@pytest.fixture()
def isolated_database():
    raw = os.getenv("PHASE11_TEST_MYSQL_DSN")
    if not raw:
        pytest.skip("PHASE11_TEST_MYSQL_DSN is required")
    base = make_url(raw)
    admin = create_engine(base.set(database=None), pool_pre_ping=True)
    name = "phase11_it_" + uuid.uuid4().hex[:12]
    with admin.begin() as conn:
        runner.ensure_mysql8(conn)
        conn.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    dsn = base.set(database=name).render_as_string(hide_password=False)
    try:
        yield dsn
    finally:
        with admin.begin() as conn:
            # Tests deliberately create independent engines to exercise the
            # runner's process boundaries.  Close their pooled sleepers before
            # dropping the one-off schema so cleanup cannot wait on an MDL.
            ids = conn.execute(text("""SELECT ID FROM information_schema.processlist
              WHERE DB=:name AND ID<>CONNECTION_ID()"""), {"name": name}).scalars().all()
            for process_id in ids:
                try:
                    conn.exec_driver_sql(f"KILL {int(process_id)}")
                except OperationalError as exc:
                    if not exc.orig.args or exc.orig.args[0] != 1094:
                        raise
            conn.exec_driver_sql(f"DROP DATABASE `{name}`")
        admin.dispose()


def _load_sql(dsn: str, path: Path) -> None:
    engine = create_engine(dsn)
    with engine.begin() as conn:
        for statement in runner.split_sql(path.read_text(encoding="utf-8")):
            conn.exec_driver_sql(statement)


def _mark_manifest_prerequisites_succeeded(dsn: str) -> None:
    _, steps = runner.check_manifest()
    seed(dsn, apply=True)
    engine = create_engine(dsn)
    with engine.begin() as conn:
        for step in steps:
            if step["stage"] not in {"pre_cutover", "post_cutover"}:
                continue
            conn.execute(text("""INSERT INTO phase11_migration_ledger
              (migration_key,script_sha256,stage,kind,status,attempt,
               last_statement_ordinal,executed_by,verification_digest)
              VALUES (:key,:sha,:stage,:kind,'succeeded',1,0,'phase11-test',:digest)
              ON DUPLICATE KEY UPDATE script_sha256=VALUES(script_sha256),
                stage=VALUES(stage),kind=VALUES(kind),status='succeeded',
                verification_digest=VALUES(verification_digest)"""), {
                    "key": step["key"], "sha": step["sha256"],
                    "stage": step["stage"], "kind": step["kind"],
                    "digest": "0" * 64 if step["kind"] == "python" else None,
                })


def _prepare_verified_down_state(dsn: str) -> tuple[dict, list[dict]]:
    """Create manifest evidence equivalent to a completed verify stage."""
    doc, steps = runner.check_manifest()
    seed(dsn, apply=True)
    engine = create_engine(dsn)
    with engine.begin() as conn:
        for step in steps:
            if step["stage"] == "down":
                continue
            conn.execute(text("""INSERT INTO phase11_migration_ledger
              (migration_key,script_sha256,stage,kind,status,attempt,
               last_statement_ordinal,cutover_resume_id,executed_by,verification_digest)
              VALUES (:key,:sha,:stage,:kind,:status,1,0,:cutover,'phase11-test',:digest)"""), {
                "key": step["key"], "sha": step["sha256"],
                "stage": step["stage"], "kind": step["kind"],
                "status": "verified" if step["stage"] == "verify" else "succeeded",
                "cutover": 0 if step["stage"] == "post_cutover" else None,
                "digest": "0" * 64 if step["kind"] in {"python", "verify_sql"} else None,
            })
    return doc, steps


def test_phase11_orm_create_all_shapes_match_engine_canonical_ddl(isolated_database):
    engine = create_engine(isolated_database)
    models = (
        Phase11MigrationLedger, ResumeReplacement,
        ResumeReplacementRolloutAssignment, ResumeMediaIsolationIssue,
        Phase11ResumeMediaKeyScan, Phase11ResumeLifecycleBackup,
    )
    for model in models:
        model.__table__.create(engine)
    statements = runner.split_sql(
        (BACKEND / "sql" / "migrations" / "phase11_001_resume_lifecycle_additive.sql").read_text(
            encoding="utf-8"
        )
    )
    expected = {
        runner._created_table_name(statement): statement
        for statement in statements if runner._created_table_name(statement)
    }
    for model in models:
        assert runner._validate_existing_create_table_shape(
            engine, expected[model.__tablename__]
        ), model.__tablename__


def test_resume_orm_create_all_emits_mysql_server_on_update(isolated_database):
    engine = create_engine(isolated_database)
    User.__table__.create(engine)
    Resume.__table__.create(engine)
    with engine.connect() as conn:
        row = conn.execute(text("""SELECT DATA_TYPE,DATETIME_PRECISION,COLUMN_DEFAULT,EXTRA
          FROM information_schema.columns WHERE table_schema=DATABASE()
            AND table_name='resume' AND column_name='updated_at'""")).one()
    assert tuple(row) == (
        "datetime", 6, "CURRENT_TIMESTAMP(6)",
        "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(6)",
    )


@pytest.mark.parametrize("revision,accepted", [
    (-1, False), (0, False), (1, True),
    (18_446_744_073_709_551_615, True),
    (18_446_744_073_709_551_616, False),
    (1.5, False), ("1", False),
])
def test_sql_seed_and_verify_share_strict_allowlist_revision_contract(
    isolated_database, revision, accepted,
):
    engine = create_engine(isolated_database)
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    value = __import__("json").dumps(
        {"revision": revision, "userids": []}, separators=(",", ":")
    )
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO system_config
          (config_key,config_value,value_type,description)
          VALUES ('resume.replacement.rollout.allowlist',:value,'json','test')
          ON DUPLICATE KEY UPDATE config_value=VALUES(config_value),value_type='json'"""),
          {"value": value})
    sql = (BACKEND / "sql" / "migrations" / "phase11_002_resume_config_seed.sql").read_text(
        encoding="utf-8"
    )
    def execute_seed():
        with engine.connect() as conn:
            for statement in runner.split_sql(sql):
                if statement.startswith("SIGNAL"):
                    blockers = int(conn.exec_driver_sql(
                        "SELECT COALESCE(@phase11_config_blockers,1)"
                    ).scalar_one())
                    if not blockers:
                        continue
                conn.exec_driver_sql(statement)
            conn.commit()

    if not accepted:
        with pytest.raises(Exception, match="phase11_config_seed_invalid|1644"):
            execute_seed()
    else:
        execute_seed()
    with engine.connect() as conn:
        summary = conn.execute(text(
            (BACKEND / "sql" / "migrations" / "phase11_resume_verify.sql").read_text(
                encoding="utf-8"
            )
        )).scalar_one()
    parsed = summary if isinstance(summary, dict) else __import__("json").loads(summary)
    assert (int(parsed["config_anomaly_count"]) == 0) is accepted


@pytest.mark.parametrize("mutation", [
    "SET NEW.raw_text=CONCAT(NEW.raw_text,' drift')",
    "SET NEW.age=NEW.age+1",
    "SET NEW.images=JSON_ARRAY_APPEND(NEW.images,'$', 'resume/drift.jpg')",
    "SET NEW.extra=JSON_SET(COALESCE(NEW.extra,JSON_OBJECT()),'$.drift',TRUE)",
])
def test_lifecycle_backfill_rejects_any_business_column_drift(isolated_database, mutation):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO user(external_userid,role) VALUES ('u1','worker')")
        conn.exec_driver_sql("""INSERT INTO system_config
          (config_key,config_value,value_type,description)
          VALUES ('ttl.resume.candidate.days','7','int','test')""")
        conn.exec_driver_sql("""INSERT INTO resume
          (owner_userid,expected_cities,expected_job_categories,salary_expect_floor_monthly,
           gender,age,raw_text,images,audit_status,expires_at,extra)
          VALUES ('u1',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,'男',25,'source',
            JSON_ARRAY('resume/a.jpg'),'passed',UTC_TIMESTAMP(6)+INTERVAL 30 DAY,
            JSON_OBJECT('nested',JSON_OBJECT('a',1)))""")
        conn.execute(text("""INSERT INTO phase11_migration_ledger
          (migration_key,script_sha256,stage,kind,status,attempt,last_statement_ordinal,
           started_at,cutover_resume_id,executed_by)
          VALUES ('phase11_resume_lifecycle_backfill',:sha,'post_cutover','python',
            'running',1,0,UTC_TIMESTAMP(6),1,'test')"""), {"sha": "a" * 64})
        conn.exec_driver_sql(f"""CREATE TRIGGER phase11_test_business_drift
          BEFORE UPDATE ON resume FOR EACH ROW {mutation}""")
    with pytest.raises(RuntimeError, match="backfill_business_fields_changed"):
        backfill(isolated_database, apply=True, batch_size=10, cursor=0)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT activated_at,raw_text,age,images,extra FROM resume WHERE id=1")).one()
        assert row[0] is None
        assert row[1] == "source"
        assert row[2] == 25


def test_lifecycle_backfill_preserves_every_derived_business_column(isolated_database):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO user(external_userid,role) VALUES ('u1','worker')")
        conn.exec_driver_sql("""INSERT INTO system_config
          (config_key,config_value,value_type,description)
          VALUES ('ttl.resume.candidate.days','7','int','test')""")
        conn.exec_driver_sql("""INSERT INTO resume
          (owner_userid,expected_cities,expected_job_categories,salary_expect_floor_monthly,
           gender,age,raw_text,images,miniprogram_url,audit_status,expires_at,extra)
          VALUES ('u1',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,'男',25,'a|NULL',
            JSON_ARRAY('resume/a.jpg'),NULL,'passed',UTC_TIMESTAMP(6)+INTERVAL 30 DAY,
            JSON_OBJECT('null_text','null','actual_null',NULL))""")
        conn.execute(text("""INSERT INTO phase11_migration_ledger
          (migration_key,script_sha256,stage,kind,status,attempt,last_statement_ordinal,
           started_at,cutover_resume_id,executed_by)
          VALUES ('phase11_resume_lifecycle_backfill',:sha,'post_cutover','python',
            'running',1,0,UTC_TIMESTAMP(6),1,'test')"""), {"sha": "a" * 64})
        before = conn.execute(text("SELECT * FROM resume WHERE id=1")).mappings().one()
    result = backfill(isolated_database, apply=True, batch_size=10, cursor=0)
    assert result["status"] == "succeeded"
    assert result["audit_summary"]["bounded_row_count"] == 1
    assert result["audit_summary"]["verified_business_rows"] == 1
    assert len(result["audit_summary"]["business_verification_digest"]) == 64
    with engine.connect() as conn:
        after = conn.execute(text("SELECT * FROM resume WHERE id=1")).mappings().one()
        stored_cursor = conn.execute(text("""SELECT resume_cursor_json
          FROM phase11_migration_ledger
          WHERE migration_key='phase11_resume_lifecycle_backfill'""")).scalar_one()
    if isinstance(stored_cursor, str):
        stored_cursor = json.loads(stored_cursor)
    assert stored_cursor["verified_business_rows"] == 1
    assert stored_cursor["business_verification_digest"] == result["audit_summary"][
        "business_verification_digest"
    ]
    excluded = {
        "id", "version", "created_at", "updated_at", "audit_status", "audit_reason",
        "audited_by", "audited_at", "activated_at", "candidate_expires_at", "expires_at",
        "delist_reason", "deleted_at",
    }
    assert {key: before[key] for key in before if key not in excluded} == {
        key: after[key] for key in after if key not in excluded
    }


@pytest.mark.parametrize(
    ("step_key", "expected_field", "expected_total"),
    [
        ("phase11_resume_lifecycle_backfill", "scanned", 3),
        ("phase11_resume_media_reconcile", "projection_scanned", 3),
        ("phase11_resume_orphan_target_reconcile", "found", 3),
        ("phase11_resume_deleted_target_backfill", "found", 3),
    ],
)
def test_python_batch_checkpoint_survives_process_crash_with_full_run_audit(
    isolated_database, step_key, expected_field, expected_total,
):
    """First committed batch must carry its cursor and audit proof atomically."""
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _mark_manifest_prerequisites_succeeded(isolated_database)
    _, steps = runner.check_manifest()
    step = next(item for item in steps if item["key"] == step_key)
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""INSERT INTO user(external_userid,role) VALUES
          ('checkpoint-u1','worker'),('checkpoint-u2','worker'),('checkpoint-u3','worker')""")
        if step_key in {
            "phase11_resume_lifecycle_backfill", "phase11_resume_media_reconcile",
            "phase11_resume_deleted_target_backfill",
        }:
            deleted = step_key == "phase11_resume_deleted_target_backfill"
            for resume_id in range(1, 4):
                conn.execute(text("""INSERT INTO resume
                  (id,owner_userid,expected_cities,expected_job_categories,
                   salary_expect_floor_monthly,gender,age,raw_text,images,audit_status,
                   audited_at,expires_at,deleted_at)
                  VALUES(:id,:owner,JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,'男',25,
                    :raw,JSON_ARRAY(:image),'passed',UTC_TIMESTAMP(6),
                    UTC_TIMESTAMP(6)+INTERVAL 30 DAY,
                    IF(:deleted,UTC_TIMESTAMP(6),NULL))"""), {
                      "id": resume_id, "owner": f"checkpoint-u{resume_id}",
                      "raw": f"resume-{resume_id}",
                      "image": f"resume/checkpoint-{resume_id}.jpg",
                      "deleted": int(deleted),
                  })
        if step_key == "phase11_resume_orphan_target_reconcile":
            conn.execute(text("""INSERT INTO recommendation_exposure_daily
              (stat_date,target_type,target_id,impression_count) VALUES
              (CURRENT_DATE(),'resume',1001,1),(CURRENT_DATE(),'resume',1002,1),
              (CURRENT_DATE(),'resume',1003,1)"""))
        conn.execute(text("""UPDATE phase11_migration_ledger SET status='running',
          resume_cursor_json=JSON_OBJECT(),verification_digest=NULL,
          started_at=UTC_TIMESTAMP(6),cutover_resume_id=:cutover
          WHERE migration_key=:key"""), {
              "key": step_key,
              "cutover": 3 if step_key == "phase11_resume_lifecycle_backfill" else None,
          })

    command = [
        sys.executable, str(step["file"]), "--dsn", isolated_database, "--apply",
        "--batch-size", "1", "--resume-cursor-json", "{}",
    ]
    redis_dsn = os.environ["PHASE11_TEST_REDIS_DSN"]
    redis_namespace = "phase11-crash-" + uuid.uuid4().hex
    if step_key == "phase11_resume_orphan_target_reconcile":
        command.extend(["--redis-dsn", redis_dsn, "--redis-namespace", redis_namespace])
    crash_env = dict(os.environ)
    crash_env["PHASE11_TEST_CRASH_AFTER_CHECKPOINT"] = "1"
    crashed = subprocess.run(command, capture_output=True, text=True, env=crash_env, check=False)
    assert crashed.returncode == 86

    with engine.connect() as conn:
        checkpoint = conn.execute(text("""SELECT resume_cursor_json,verification_digest
          FROM phase11_migration_ledger WHERE migration_key=:key"""),
          {"key": step_key}).mappings().one()
    checkpoint_cursor = checkpoint["resume_cursor_json"]
    if isinstance(checkpoint_cursor, str):
        checkpoint_cursor = json.loads(checkpoint_cursor)
    checkpoint_summary = checkpoint_cursor["audit_summary"]
    assert checkpoint["verification_digest"] == runner._canonical_digest(checkpoint_summary)
    assert checkpoint_summary[expected_field] == 1

    runner._run_python(
        engine, isolated_database, step, checkpoint_cursor,
        redis_dsn=redis_dsn if step_key == "phase11_resume_orphan_target_reconcile" else None,
        redis_namespace=redis_namespace if step_key == "phase11_resume_orphan_target_reconcile" else None,
    )
    with engine.begin() as conn:
        completed = conn.execute(text("""SELECT resume_cursor_json,verification_digest
          FROM phase11_migration_ledger WHERE migration_key=:key FOR UPDATE"""),
          {"key": step_key}).mappings().one()
        final_cursor = completed["resume_cursor_json"]
        if isinstance(final_cursor, str):
            final_cursor = json.loads(final_cursor)
        final_summary = final_cursor["audit_summary"]
        assert final_summary[expected_field] == expected_total
        assert completed["verification_digest"] == runner._canonical_digest(final_summary)
        conn.execute(text("""UPDATE phase11_migration_ledger SET verification_digest=:tampered
          WHERE migration_key=:key"""), {"tampered": "f" * 64, "key": step_key})

    with pytest.raises(RuntimeError, match="python_step_checkpoint_digest_mismatch"):
        runner._run_python(
            engine, isolated_database, step, final_cursor,
            redis_dsn=redis_dsn if step_key == "phase11_resume_orphan_target_reconcile" else None,
            redis_namespace=redis_namespace if step_key == "phase11_resume_orphan_target_reconcile" else None,
        )


@pytest.mark.parametrize("tamper", [
    "ALTER TABLE phase11_migration_ledger ADD COLUMN injected INT NULL",
    "ALTER TABLE phase11_resume_media_key_scan DROP KEY idx_phase11_media_scan_key",
    "ALTER TABLE phase11_resume_lifecycle_backup MODIFY updated_at DATETIME(3) NOT NULL",
    "ALTER TABLE resume DROP KEY idx_resume_hard_delete",
    "ALTER TABLE resume ADD KEY phase11_extra_candidate (candidate_expires_at)",
    "CREATE TRIGGER phase11_unknown_trigger BEFORE UPDATE ON resume FOR EACH ROW SET NEW.activated_at=NEW.activated_at",
    "ALTER TABLE resume ADD CONSTRAINT phase11_candidate_check CHECK (candidate_expires_at IS NULL OR candidate_expires_at>created_at)",
])
def test_down_preflight_rejects_every_unknown_phase11_shape(isolated_database, tamper):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    engine = create_engine(isolated_database)
    runner._down_preflight(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(tamper)
    with pytest.raises(RuntimeError, match="down_unknown_schema_dependency|duplicate_ddl_shape_mismatch"):
        runner._down_preflight(engine)


def test_config_seed_is_atomic_for_invalid_existing_value(isolated_database):
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE system_config (
          config_key VARCHAR(128) PRIMARY KEY, config_value TEXT NOT NULL,
          value_type VARCHAR(16) NOT NULL, description VARCHAR(255) NULL) ENGINE=InnoDB""")
        conn.execute(text("""INSERT INTO system_config VALUES
          ('ttl.resume.days','0','int','invalid')"""))
    with pytest.raises(RuntimeError, match="invalid_existing_resume_ttl"):
        seed(isolated_database, apply=True)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT config_key,config_value FROM system_config")).all()
    assert rows == [("ttl.resume.days", "0")]


def test_runner_check_requires_mysql_and_manifest_only_is_explicit(isolated_database):
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql(runner.LEDGER_DDL)
    assert runner.main(["manifest-check"]) == 0
    assert runner.main(["check", "--dsn", isolated_database, "--stage", "pre_cutover"]) == 0


def test_explicit_redis_target_is_durable():
    raw = os.getenv("PHASE11_TEST_REDIS_DSN")
    if not raw:
        pytest.skip("PHASE11_TEST_REDIS_DSN is required")
    import redis
    client = redis.Redis.from_url(raw, decode_responses=True)
    client.ping()
    validate_redis_durability_policy(client)


def test_media_reconcile_never_binds_shared_key_across_batches(isolated_database):
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE resume (
          id BIGINT UNSIGNED PRIMARY KEY, owner_userid VARCHAR(64) NOT NULL,
          images JSON NULL, deleted_at DATETIME(6) NULL) ENGINE=InnoDB""")
        conn.exec_driver_sql("""CREATE TABLE resume_media_isolation_issue (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
          resume_id BIGINT UNSIGNED NULL, key_hash CHAR(64) NOT NULL,
          issue_type VARCHAR(64) NOT NULL,
          status ENUM('open','approved','resolved','blocked') NOT NULL DEFAULT 'open',
          UNIQUE KEY uq_resume_media_isolation_issue (resume_id,key_hash,issue_type)
        ) ENGINE=InnoDB""")
        conn.exec_driver_sql("""CREATE TABLE phase11_resume_media_key_scan (
          resume_id BIGINT UNSIGNED NOT NULL, key_hash CHAR(64) NOT NULL,
          reference_kind ENUM('valid','invalid') NOT NULL,
          reference_count INT UNSIGNED NOT NULL,
          first_seen_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
          PRIMARY KEY(resume_id,key_hash,reference_kind), KEY(key_hash,reference_kind,resume_id)
        ) ENGINE=InnoDB""")
        conn.exec_driver_sql("""CREATE TABLE phase11_migration_ledger (
          migration_key VARCHAR(128) PRIMARY KEY, resume_cursor_json JSON NULL,
          verification_digest CHAR(64) NULL
        ) ENGINE=InnoDB""")
        conn.exec_driver_sql("""CREATE TABLE media_asset_lifecycle (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
          object_key VARCHAR(512) NOT NULL UNIQUE, operation_id CHAR(36) NULL,
          owner_userid VARCHAR(64) NOT NULL, entity_type ENUM('job','resume') NULL,
          entity_id BIGINT UNSIGNED NULL,
          state ENUM('pending','attached','delete_pending','deleted','dead_letter') NOT NULL,
          next_attempt_at DATETIME(6) NULL
        ) ENGINE=InnoDB""")
        conn.execute(text("""INSERT INTO resume(id,owner_userid,images,deleted_at) VALUES
          (1,'owner-a',JSON_ARRAY('resume/shared.jpg'),NULL),
          (2,'owner-b',JSON_ARRAY('resume/shared.jpg'),NULL),
          (3,'owner-c',JSON_ARRAY('resume/deleted.jpg'),UTC_TIMESTAMP(6))"""))
        conn.execute(text("""INSERT INTO phase11_migration_ledger
          (migration_key,resume_cursor_json)
          VALUES ('phase11_resume_media_reconcile',JSON_OBJECT())"""))

    first = reconcile(
        isolated_database, apply=True, batch_size=1, cursor=0,
        max_rows_per_second=10000,
    )
    assert first["resume_cursor"]["last_resume_id"] == 3
    assert first["resume_cursor"]["audit_summary"] == first["audit_summary"]
    with engine.connect() as conn:
        shared_issues = conn.execute(text("""SELECT resume_id,status
          FROM resume_media_isolation_issue
          WHERE issue_type='shared_reference' ORDER BY resume_id""")).all()
        lifecycle = conn.execute(text("""SELECT object_key,entity_id,state
          FROM media_asset_lifecycle ORDER BY object_key""")).all()
    assert shared_issues == [(1, "open"), (2, "open")]
    assert lifecycle == [("resume/deleted.jpg", 3, "delete_pending")]

    repeated = reconcile(
        isolated_database, apply=True, batch_size=1, cursor=first["resume_cursor"],
        max_rows_per_second=10000,
    )
    resumed = reconcile(
        isolated_database, apply=True, batch_size=1,
        cursor=first["resume_cursor"],
        max_rows_per_second=10000,
    )
    # Audit counters are cumulative evidence for the whole migration, not
    # per-process deltas.  A no-op resume must reproduce the same proof.
    assert repeated["audit_summary"] == first["audit_summary"]
    assert resumed["audit_summary"] == first["audit_summary"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM media_asset_lifecycle")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM resume_media_isolation_issue")).scalar_one() == 2


def test_pre_cutover_apply_from_legacy_shape_and_resume_are_idempotent(isolated_database):
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE resume (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
          audit_status ENUM('pending','passed','rejected') NOT NULL DEFAULT 'pending',
          expires_at DATETIME NOT NULL, version INT UNSIGNED NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          deleted_at DATETIME NULL) ENGINE=InnoDB""")
        conn.exec_driver_sql("""CREATE TABLE system_config (
          config_key VARCHAR(128) PRIMARY KEY, config_value TEXT NOT NULL,
          value_type VARCHAR(16) NOT NULL, description VARCHAR(255) NULL) ENGINE=InnoDB""")
    runner.run_stage(isolated_database, command="apply", stage="pre_cutover")
    runner.run_stage(isolated_database, command="resume", stage="pre_cutover")
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.columns
          WHERE table_schema=DATABASE() AND table_name='resume'
          AND column_name IN ('activated_at','candidate_expires_at','delist_reason')""")).scalar_one() == 3
        assert conn.execute(text("""SELECT COUNT(*) FROM phase11_migration_ledger
          WHERE stage='pre_cutover' AND status='succeeded'""")).scalar_one() == 2


@pytest.mark.parametrize("key,value", [
    ("ttl.resume.days", "0"),
    ("ttl.resume.days", "3651"),
    ("ttl.resume.candidate.days", "0"),
    ("ttl.resume.candidate.days", "366"),
    ("rollout.resume_replacement.allowlist", '["u1","u1"]'),
    ("resume.replacement.rollout.allowlist", '{"revision":1,"userids":["u1","u1"]}'),
    ("rollout.resume_replacement.allowlist", '{"revision":1,"userids":["  "]}'),
    ("rollout.resume_replacement.allowlist", '{"revision":1,"userids":[7]}'),
    ("rollout.resume_replacement.allowlist", '{"revision":1,"userids":[],"extra":true}'),
])
def test_config_seed_rejects_all_invalid_existing_values_atomically(
    isolated_database, key, value,
):
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE system_config (
          config_key VARCHAR(128) PRIMARY KEY, config_value TEXT NOT NULL,
          value_type VARCHAR(16) NOT NULL, description VARCHAR(255) NULL) ENGINE=InnoDB""")
        conn.execute(text("INSERT INTO system_config VALUES (:key,:value,'json','bad')"), {
            "key": key, "value": value,
        })
    with pytest.raises((RuntimeError, ValueError)):
        seed(isolated_database, apply=True)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT config_value FROM system_config WHERE config_key=:key"), {
            "key": key,
        }).scalar_one() == value
        assert conn.execute(text("SELECT COUNT(*) FROM system_config")).scalar_one() == 1


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("ttl.resume.days", "01"),
        ("ttl.resume.days", "+1"),
        ("ttl.resume.days", " 1"),
        ("ttl.resume.days", "1 "),
        ("ttl.resume.days", ""),
        ("ttl.resume.days", "\uff11"),
        ("ttl.resume.candidate.days", "01"),
        ("ttl.resume.candidate.days", "+1"),
        ("ttl.resume.candidate.days", "١"),
    ],
)
def test_python_config_seed_rejects_noncanonical_ttl_without_partial_seed(
    isolated_database, key, raw,
):
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE system_config (
          config_key VARCHAR(128) PRIMARY KEY, config_value TEXT NOT NULL,
          value_type VARCHAR(16) NOT NULL, description VARCHAR(255) NULL) ENGINE=InnoDB""")
        conn.execute(text("INSERT INTO system_config VALUES (:key,:raw,'int','bad')"), {
            "key": key, "raw": raw,
        })
    with pytest.raises(RuntimeError, match="invalid_existing_.*ttl"):
        seed(isolated_database, apply=True)
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT config_key,config_value,value_type FROM system_config"
        )).all() == [(key, raw, "int")]


def test_python_config_seed_fails_closed_on_valid_legacy_rollout_key(
    isolated_database,
):
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE system_config (
          config_key VARCHAR(128) PRIMARY KEY, config_value TEXT NOT NULL,
          value_type VARCHAR(16) NOT NULL, description VARCHAR(255) NULL) ENGINE=InnoDB""")
        conn.execute(text("""INSERT INTO system_config VALUES
          ('rollout.resume_replacement.allowlist',:value,'json','legacy')"""), {
            "value": '{"revision":1,"userids":[]}',
        })
    with pytest.raises(RuntimeError, match="legacy_rollout_config_present"):
        seed(isolated_database, apply=True)
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT config_key,config_value FROM system_config"
        )).all() == [(
            "rollout.resume_replacement.allowlist",
            '{"revision":1,"userids":[]}',
        )]


def test_verify_rejects_media_missing_and_every_orphan_source(isolated_database):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _mark_manifest_prerequisites_succeeded(isolated_database)
    engine = create_engine(isolated_database)

    # Empty canonical schema is a valid deterministic summary.
    assert len(runner.verify(isolated_database)) == 64
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM phase11_migration_ledger WHERE stage='verify'"))
        conn.execute(text("""INSERT INTO phase11_resume_media_key_scan
          (resume_id,key_hash,reference_kind,reference_count)
          VALUES (999999,:hash,'valid',1)"""), {"hash": "a" * 64})
    with pytest.raises(RuntimeError, match="media_registry_projection_drift|verify_anomalies_present"):
        runner.verify(isolated_database)

    # The durable source-family union must detect a target that exists only in
    # recommendation_exposure_daily, without relying on request JSON as a superset.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM phase11_resume_media_key_scan"))
        conn.execute(text("""INSERT INTO recommendation_exposure_daily
          (stat_date,target_type,target_id,impression_count)
          VALUES (UTC_DATE(),'resume',999998,1)"""))
    with pytest.raises(RuntimeError, match="verify_anomalies_present"):
        runner.verify(isolated_database)


@pytest.mark.parametrize("missing_key", [
    "ttl.resume.days",
    "ttl.resume.candidate.days",
    "resume.replacement.rollout.allowlist",
])
def test_final_verify_requires_each_config_key_exactly_once(
    isolated_database, missing_key,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _mark_manifest_prerequisites_succeeded(isolated_database)
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM system_config WHERE config_key=:key"), {
            "key": missing_key,
        })
    with pytest.raises(RuntimeError, match="verify_anomalies_present"):
        runner.verify(isolated_database)


def test_check_is_read_only_and_requires_explicit_dsn(isolated_database):
    engine = create_engine(isolated_database)
    assert runner.main(["check", "--dsn", isolated_database]) == 0
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
          WHERE table_schema=DATABASE() AND table_name='phase11_migration_ledger'""")).scalar_one() == 0
    with pytest.raises(SystemExit):
        runner.main(["check"])


def test_pre_cutover_sql_seed_preserves_legal_winner_and_rejects_legacy_key(
    isolated_database,
):
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE resume (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
          audit_status ENUM('pending','passed','rejected') NOT NULL DEFAULT 'pending',
          expires_at DATETIME NOT NULL, version INT UNSIGNED NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          deleted_at DATETIME NULL) ENGINE=InnoDB""")
        conn.exec_driver_sql("""CREATE TABLE system_config (
          config_key VARCHAR(128) PRIMARY KEY, config_value TEXT NOT NULL,
          value_type ENUM('string','int','bool','json') NOT NULL,
          description VARCHAR(255) NULL) ENGINE=InnoDB""")
        conn.exec_driver_sql("""INSERT INTO system_config VALUES
          ('ttl.resume.days','45','int','operator value'),
          ('ttl.resume.candidate.days','9','int','operator value')""")
        conn.execute(text("""INSERT INTO system_config VALUES
          ('resume.replacement.rollout.allowlist',:allowlist,'json','operator value')"""),
          {"allowlist": '{"revision":2,"userids":["u1"]}'})
    runner.run_stage(isolated_database, command="apply", stage="pre_cutover")
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT config_value FROM system_config
          WHERE config_key='ttl.resume.days'""")).scalar_one() == "45"

    # A pre-freeze spelling is ambiguous state, never silently migrated.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM phase11_migration_ledger WHERE migration_key='phase11_resume_config_seed'"))
        conn.execute(text("""INSERT INTO system_config VALUES
          ('rollout.resume_replacement.allowlist','{}','json','legacy')"""))
    with pytest.raises(Exception, match="phase11_config_seed_invalid|1644"):
        runner.run_stage(isolated_database, command="apply", stage="pre_cutover")
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT COUNT(*) FROM system_config
          WHERE config_key='rollout.resume_replacement.allowlist'""")).scalar_one() == 1


def test_verify_uses_two_independent_snapshots_and_detects_intervening_write(
    isolated_database, monkeypatch,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _mark_manifest_prerequisites_succeeded(isolated_database)
    engine = create_engine(isolated_database)

    def race(_engine):
        with engine.begin() as conn:
            conn.execute(text("""INSERT INTO phase11_resume_media_key_scan
              (resume_id,key_hash,reference_kind,reference_count)
              VALUES (999999,:hash,'valid',1)"""), {"hash": "b" * 64})

    monkeypatch.setattr(runner, "_between_verify_snapshots", race)
    with pytest.raises(RuntimeError, match="media_registry_projection_drift|verify_summary_changed"):
        runner.verify(isolated_database)


def test_resolved_shared_media_issue_requires_actual_detach_and_rescan(
    isolated_database,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _mark_manifest_prerequisites_succeeded(isolated_database)
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_migration_ledger
          SET status='running',resume_cursor_json=JSON_OBJECT(),verification_digest=NULL
          WHERE migration_key='phase11_resume_media_reconcile'"""))
        conn.exec_driver_sql("""INSERT INTO user(external_userid,role) VALUES
          ('u1','worker'),('u2','worker')""")
        conn.execute(text("""INSERT INTO resume
          (id,owner_userid,expected_cities,expected_job_categories,
           salary_expect_floor_monthly,gender,age,raw_text,images,audit_status,
           candidate_expires_at)
          VALUES
          (1,'u1',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,'男',25,'r1',
           JSON_ARRAY('resume/shared.jpg'),'pending',UTC_TIMESTAMP(6)+INTERVAL 7 DAY),
          (2,'u2',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,'女',25,'r2',
           JSON_ARRAY('resume/shared.jpg'),'pending',UTC_TIMESTAMP(6)+INTERVAL 7 DAY)"""))
    reconcile(isolated_database, apply=True, batch_size=1, cursor=0, max_rows_per_second=10000)
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_migration_ledger SET status='succeeded'
          WHERE migration_key='phase11_resume_media_reconcile'"""))
    with engine.begin() as conn:
        conn.execute(text("""UPDATE resume_media_isolation_issue
          SET status='resolved',resolved_at=UTC_TIMESTAMP(6)"""))
    with pytest.raises(RuntimeError, match="verify_anomalies_present"):
        runner.verify(isolated_database)

    with engine.begin() as conn:
        conn.execute(text("UPDATE resume SET images=JSON_ARRAY() WHERE id=1"))
        conn.execute(text("""UPDATE phase11_migration_ledger
          SET resume_cursor_json=JSON_OBJECT(),verification_digest=NULL
          WHERE migration_key='phase11_resume_media_reconcile'"""))
    reconcile(isolated_database, apply=True, batch_size=1, cursor=0, max_rows_per_second=10000)
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_migration_ledger SET status='succeeded'
          WHERE migration_key='phase11_resume_media_reconcile'"""))
        # A deliberately rerun post-cutover step produces a new audit proof;
        # require a new verify ledger entry instead of overwriting old evidence.
        conn.execute(text("""DELETE FROM phase11_migration_ledger
          WHERE migration_key='phase11_resume_verify'"""))
    assert len(runner.verify(isolated_database)) == 64

    with engine.begin() as conn:
        conn.execute(text("""UPDATE resume SET images=JSON_ARRAY(
          'resume/duplicate.jpg','resume/duplicate.jpg') WHERE id=1"""))
        conn.execute(text("""UPDATE phase11_migration_ledger
          SET resume_cursor_json=JSON_OBJECT(),verification_digest=NULL
          WHERE migration_key='phase11_resume_media_reconcile'"""))
    reconcile(isolated_database, apply=True, batch_size=1, cursor=0, max_rows_per_second=10000)
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_migration_ledger SET status='succeeded'
          WHERE migration_key='phase11_resume_media_reconcile'"""))
    with engine.begin() as conn:
        conn.execute(text("""UPDATE resume_media_isolation_issue SET status='resolved',
          resolved_at=UTC_TIMESTAMP(6) WHERE issue_type='duplicate_reference'"""))
    with pytest.raises(RuntimeError, match="verify_anomalies_present"):
        runner.verify(isolated_database)

    with engine.begin() as conn:
        conn.execute(text("UPDATE resume SET images=JSON_ARRAY(123) WHERE id=1"))
        conn.execute(text("""UPDATE phase11_migration_ledger
          SET resume_cursor_json=JSON_OBJECT(),verification_digest=NULL
          WHERE migration_key='phase11_resume_media_reconcile'"""))
    reconcile(isolated_database, apply=True, batch_size=1, cursor=0, max_rows_per_second=10000)
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_migration_ledger SET status='succeeded'
          WHERE migration_key='phase11_resume_media_reconcile'"""))
    with engine.begin() as conn:
        conn.execute(text("""UPDATE resume_media_isolation_issue SET status='resolved',
          resolved_at=UTC_TIMESTAMP(6) WHERE issue_type='invalid_reference'"""))
    with pytest.raises(RuntimeError, match="verify_anomalies_present"):
        runner.verify(isolated_database)

    with engine.begin() as conn:
        conn.execute(text("UPDATE resume SET images=JSON_ARRAY() WHERE id=1"))
        conn.execute(text("""UPDATE phase11_migration_ledger
          SET resume_cursor_json=JSON_OBJECT(),verification_digest=NULL
          WHERE migration_key='phase11_resume_media_reconcile'"""))
    reconcile(isolated_database, apply=True, batch_size=1, cursor=0, max_rows_per_second=10000)
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_migration_ledger SET status='succeeded'
          WHERE migration_key='phase11_resume_media_reconcile'"""))
        conn.execute(text("""DELETE FROM phase11_migration_ledger
          WHERE migration_key='phase11_resume_verify'"""))
    assert len(runner.verify(isolated_database)) == 64


@pytest.mark.parametrize("backup_table", [
    "phase11_resume_down_backup",
    "phase11_resume_down_replacement_backup",
    "phase11_resume_down_assignment_backup",
    "phase11_resume_down_media_issue_backup",
    "phase11_resume_down_ledger_backup",
])
def test_down_backup_insert_commit_before_ordinal_is_replay_safe(
    isolated_database, monkeypatch, backup_table,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _, steps = runner.check_manifest()
    down = next(step for step in steps if step["stage"] == "down")
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO user(external_userid,role) VALUES ('u1','worker')")
        conn.exec_driver_sql("""INSERT INTO system_config
          (config_key,config_value,value_type,description)
          VALUES ('ttl.resume.days','30','int','test')""")
        for step in steps:
            if step["stage"] == "down":
                continue
            conn.execute(text("""INSERT INTO phase11_migration_ledger
              (migration_key,script_sha256,stage,kind,status,attempt,last_statement_ordinal,executed_by)
              VALUES (:key,:sha,:stage,:kind,:status,1,0,'phase11-test')"""), {
                "key": step["key"], "sha": step["sha256"], "stage": step["stage"],
                "kind": step["kind"], "status": "verified" if step["stage"] == "verify" else "succeeded",
            })
        conn.execute(text("""INSERT INTO phase11_migration_ledger
          (migration_key,script_sha256,stage,kind,status,attempt,last_statement_ordinal,executed_by)
          VALUES (:key,:sha,'down','sql','running',1,0,'phase11-test')"""),
          {"key": down["key"], "sha": down["sha256"]})
        conn.execute(text("""INSERT INTO resume
          (owner_userid,expected_cities,expected_job_categories,salary_expect_floor_monthly,
           gender,age,raw_text,audit_status,candidate_expires_at)
          VALUES ('u1',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,'男',25,'r','pending',
            UTC_TIMESTAMP(6)+INTERVAL 7 DAY)"""))
        conn.execute(text("""INSERT INTO resume_replacement
          (operation_id,source_msg_id,owner_userid,old_resume_id,new_resume_id,
           old_resume_version,old_business_digest,lifecycle_status)
          VALUES (UUID(),'m1','u1',1,2,1,:digest,'closed')"""), {"digest": "c" * 64})
        conn.execute(text("""INSERT INTO resume_replacement_rollout_assignment
          (operation_id,owner_userid,cohort,allowlist_revision,source_msg_id)
          VALUES (UUID(),'u1','control',1,'m1')"""))
        conn.execute(text("""INSERT INTO resume_media_isolation_issue
          (resume_id,key_hash,issue_type,status,resolved_at)
          VALUES (1,:hash,'invalid_reference','resolved',UTC_TIMESTAMP(6))"""), {"hash": "d" * 64})

    crashed = False
    def crash_after_commit(_step, _ordinal, statement):
        nonlocal crashed
        if not crashed and f"REPLACE INTO `{backup_table}`" in statement:
            crashed = True
            raise RuntimeError("simulated_commit_before_ordinal")

    monkeypatch.setattr(runner, "_after_sql_commit_before_checkpoint", crash_after_commit)
    with pytest.raises(RuntimeError, match="simulated_commit_before_ordinal"):
        runner._run_sql(engine, down, 0)
    monkeypatch.setattr(runner, "_after_sql_commit_before_checkpoint", lambda *_: None)
    with engine.connect() as conn:
        ordinal = int(conn.execute(text("""SELECT last_statement_ordinal
          FROM phase11_migration_ledger WHERE migration_key=:key"""), {"key": down["key"]}).scalar_one())
    runner._run_sql(engine, down, ordinal)
    with engine.connect() as conn:
        pairs = conn.execute(text("""SELECT COUNT(*) FROM (
          SELECT SUBSTRING(artifact_name,8) name,row_count,row_digest
          FROM phase11_resume_down_export_audit WHERE LEFT(artifact_name,7)='source_') s
          JOIN (SELECT SUBSTRING(artifact_name,8) name,row_count,row_digest
          FROM phase11_resume_down_export_audit WHERE LEFT(artifact_name,7)='backup_') b USING(name)
          WHERE s.row_count<>b.row_count OR s.row_digest<>b.row_digest""")).scalar_one()
        assert pairs == 0
    non_key_mutations = {
        "phase11_resume_down_backup": "UPDATE phase11_resume_down_backup SET raw_text='tampered' LIMIT 1",
        "phase11_resume_down_replacement_backup": "UPDATE phase11_resume_down_replacement_backup SET conflict_reason='tampered' LIMIT 1",
        "phase11_resume_down_assignment_backup": "UPDATE phase11_resume_down_assignment_backup SET owner_userid='tampered' LIMIT 1",
        "phase11_resume_down_media_issue_backup": "UPDATE phase11_resume_down_media_issue_backup SET approval_reason='tampered' LIMIT 1",
        "phase11_resume_down_ledger_backup": "UPDATE phase11_resume_down_ledger_backup SET error_code='tampered' LIMIT 1",
    }
    with engine.begin() as conn:
        conn.exec_driver_sql(non_key_mutations[backup_table])
    total = len(runner.split_sql(down["file"].read_text(encoding="utf-8")))
    with pytest.raises(RuntimeError, match="down_export_audit_drift"):
        runner._run_sql(engine, down, total)


def test_down_executes_with_guarded_backups_and_replays_final_checkpoint(
    isolated_database,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    seed(isolated_database, apply=True)
    _, steps = runner.check_manifest()
    down = next(step for step in steps if step["stage"] == "down")
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        for step in steps:
            if step["stage"] == "down":
                continue
            conn.execute(text("""INSERT INTO phase11_migration_ledger
              (migration_key,script_sha256,stage,kind,status,attempt,
               last_statement_ordinal,executed_by)
              VALUES (:key,:sha,:stage,:kind,:status,1,0,'phase11-test')"""), {
                "key": step["key"], "sha": step["sha256"],
                "stage": step["stage"], "kind": step["kind"],
                "status": "verified" if step["stage"] == "verify" else "succeeded",
            })
        conn.execute(text("""INSERT INTO phase11_migration_ledger
          (migration_key,script_sha256,stage,kind,status,attempt,
           last_statement_ordinal,executed_by)
          VALUES (:key,:sha,'down','sql','running',1,0,'phase11-test')"""), {
            "key": down["key"], "sha": down["sha256"],
        })
        # Models a MySQL DDL commit followed by process death before the CREATE
        # LIKE ordinal and its captured-shape cursor were checkpointed.
        conn.exec_driver_sql("""CREATE TABLE phase11_resume_down_assignment_backup
          LIKE resume_replacement_rollout_assignment""")
    runner._run_sql(engine, down, 0)
    total = len(runner.split_sql(down["file"].read_text(encoding="utf-8")))
    with engine.connect() as conn:
        ordinal = conn.execute(text("""SELECT last_statement_ordinal
          FROM phase11_migration_ledger WHERE migration_key=:key"""), {
            "key": down["key"],
        }).scalar_one()
        assert ordinal == total
        assert conn.execute(text("SELECT COUNT(*) FROM phase11_resume_down_export_audit")).scalar_one() == 10
    # Simulates process death after the final durable ordinal: replay must
    # reconcile destructive statements and verify the preserved backups.
    runner._run_sql(engine, down, total)
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
          WHERE table_schema=DATABASE() AND table_name IN
          ('phase11_down_guard','phase11_export_guard','phase11_null_ttl_guard')""")).scalar_one() == 0

    # The retained audit is itself rollback evidence.  A later replay must not
    # accept a summary whose digest was altered after the source tables fell
    # out of the old schema.
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_resume_down_export_audit
          SET row_digest=:digest WHERE artifact_name='backup_assignment'"""), {
            "digest": "0" * 64,
        })
    with pytest.raises(RuntimeError, match="down_export_audit_drift"):
        runner._run_sql(engine, down, total)
    with engine.begin() as conn:
        conn.execute(text("""UPDATE phase11_resume_down_export_audit b
          JOIN phase11_resume_down_export_audit s
            ON s.artifact_name='source_assignment'
          SET b.row_digest=s.row_digest
          WHERE b.artifact_name='backup_assignment'"""))

        conn.execute(text("""INSERT INTO phase11_resume_down_export_audit
          (artifact_name,row_count,row_digest)
          VALUES ('unexpected_artifact',0,:digest)"""), {"digest": "0" * 64})
    with pytest.raises(RuntimeError, match="down_export_audit_drift"):
        runner._run_sql(engine, down, total)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM phase11_resume_down_export_audit WHERE artifact_name='unexpected_artifact'"))
        conn.execute(text("DELETE FROM phase11_resume_down_export_audit WHERE artifact_name='backup_assignment'"))
    with pytest.raises(RuntimeError, match="down_export_audit_drift"):
        runner._run_sql(engine, down, total)
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO phase11_resume_down_export_audit
          (artifact_name,row_count,row_digest)
          SELECT 'backup_assignment',row_count,row_digest
          FROM phase11_resume_down_export_audit
          WHERE artifact_name='source_assignment'"""))

    # A completed temporary guard is allowed to be absent, but a persistent
    # backup still has to match the exact shape captured before source drop.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE phase11_resume_down_assignment_backup ADD COLUMN injected INT NULL"
        )
    with pytest.raises(RuntimeError, match="duplicate_ddl_shape_mismatch"):
        runner._run_sql(engine, down, total)


@pytest.mark.parametrize("destructive_index", range(12))
def test_public_down_resume_recovers_every_destructive_commit_window(
    isolated_database, monkeypatch, destructive_index,
):
    """A new runner process must resume every DROP/ALTER checkpoint window."""
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _, steps = runner.check_manifest()
    down = next(step for step in steps if step["stage"] == "down")
    engine = create_engine(isolated_database)
    seed(isolated_database, apply=True)
    with engine.begin() as conn:
        for step in steps:
            if step["stage"] == "down":
                continue
            conn.execute(text("""INSERT INTO phase11_migration_ledger
              (migration_key,script_sha256,stage,kind,status,attempt,
               last_statement_ordinal,cutover_resume_id,executed_by,verification_digest)
              VALUES (:key,:sha,:stage,:kind,:status,1,0,:cutover,'phase11-test',:digest)"""), {
                "key": step["key"], "sha": step["sha256"],
                "stage": step["stage"], "kind": step["kind"],
                "status": "verified" if step["stage"] == "verify" else "succeeded",
                "cutover": 0 if step["stage"] == "post_cutover" else None,
                "digest": "0" * 64 if step["kind"] in {"python", "verify_sql"} else None,
            })

    destructive = [
        statement for statement in runner.split_sql(down["file"].read_text(encoding="utf-8"))
        if statement.lstrip().upper().startswith(("DROP TABLE", "ALTER TABLE"))
    ]
    assert len(destructive) == 12
    target = destructive[destructive_index]
    crashed = False

    def crash_after_commit(_step, _ordinal, statement):
        nonlocal crashed
        if not crashed and statement == target:
            crashed = True
            raise RuntimeError("simulated_destructive_commit_before_ordinal")

    monkeypatch.setattr(runner, "_probe_builds", lambda *_: "b" * 64)
    monkeypatch.setattr(runner, "_after_sql_commit_before_checkpoint", crash_after_commit)
    with pytest.raises(RuntimeError, match="simulated_destructive_commit_before_ordinal"):
        runner.run_stage(
            isolated_database, command="apply", stage="down",
            probes=["http://isolated.invalid=" + "a" * 40],
            cutover_resume_id=0, confirm_down=True,
        )
    monkeypatch.setattr(runner, "_after_sql_commit_before_checkpoint", lambda *_: None)
    assert runner.main([
        "resume", "--stage", "down", "--dsn", isolated_database,
        "--build-probe-url", "http://isolated.invalid=" + "a" * 40,
        "--cutover-resume-id", "0", "--confirm-down",
    ]) == 0
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT status FROM phase11_migration_ledger
          WHERE migration_key='phase11_resume_lifecycle_down'""")).scalar_one() == "succeeded"


@pytest.mark.parametrize("target_ordinal", [27, 28])
@pytest.mark.parametrize("crash_window", ["commit_before_ordinal", "after_checkpoint"])
def test_public_down_resume_recovers_ttl_dml_commit_windows(
    isolated_database, monkeypatch, target_ordinal, crash_window,
):
    """Both expiry DMLs survive either process-death boundary."""
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _, steps = _prepare_verified_down_state(isolated_database)
    down = next(step for step in steps if step["stage"] == "down")
    statements = runner.split_sql(down["file"].read_text(encoding="utf-8"))
    target = statements[target_ordinal - 1]
    assert target.lstrip().upper().startswith("UPDATE `RESUME`")
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO user(external_userid,role) VALUES ('dml-user','worker')")
        candidate = "NULL" if target_ordinal == 27 else "'2036-02-03 04:05:06.000000'"
        conn.exec_driver_sql(f"""INSERT INTO resume
          (id,owner_userid,expected_cities,expected_job_categories,
           salary_expect_floor_monthly,gender,age,raw_text,audit_status,
           activated_at,candidate_expires_at,expires_at)
          VALUES (7001,'dml-user',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,
           '男',25,'business-canary','pending',NULL,{candidate},NULL)""")
        lifecycle_expiry = "'2035-01-02 03:04:05.000000'" if target_ordinal == 27 else "NULL"
        conn.exec_driver_sql(f"""INSERT INTO phase11_resume_lifecycle_backup
          (resume_id,expires_at,activated_at,candidate_expires_at,deleted_at,version,updated_at)
          SELECT id,{lifecycle_expiry},activated_at,candidate_expires_at,deleted_at,version,updated_at
          FROM resume WHERE id=7001""")

    crashed = False

    def crash(_step, _ordinal, statement):
        nonlocal crashed
        if not crashed and statement == target:
            crashed = True
            raise RuntimeError("simulated_ttl_dml_process_death")

    monkeypatch.setattr(runner, "_probe_builds", lambda *_: "b" * 64)
    hook = (
        "_after_sql_commit_before_checkpoint"
        if crash_window == "commit_before_ordinal" else "_after_sql_checkpoint"
    )
    monkeypatch.setattr(runner, hook, crash)
    with pytest.raises(RuntimeError, match="simulated_ttl_dml_process_death"):
        runner.run_stage(
            isolated_database, command="apply", stage="down",
            probes=["http://isolated.invalid=" + "a" * 40],
            cutover_resume_id=0, confirm_down=True,
        )
    monkeypatch.setattr(runner, hook, lambda *_: None)

    # Exercise the public resume entry point, representing a fresh runner
    # process reading only durable MySQL state.
    assert runner.main([
        "resume", "--stage", "down", "--dsn", isolated_database,
        "--build-probe-url", "http://isolated.invalid=" + "a" * 40,
        "--cutover-resume-id", "0", "--confirm-down",
    ]) == 0
    with engine.connect() as conn:
        expires_at = conn.execute(text(
            "SELECT expires_at FROM phase11_resume_down_backup WHERE id=7001"
        )).scalar_one()
        assert expires_at is None  # retained pre-down evidence is unchanged
        assert conn.execute(text("""SELECT status FROM phase11_migration_ledger
          WHERE migration_key='phase11_resume_lifecycle_down'""")).scalar_one() == "succeeded"


def test_down_dml_resume_rejects_unrelated_business_field_drift(
    isolated_database, monkeypatch,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _, steps = _prepare_verified_down_state(isolated_database)
    down = next(step for step in steps if step["stage"] == "down")
    target = runner.split_sql(down["file"].read_text(encoding="utf-8"))[27]
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO user(external_userid,role) VALUES ('drift-user','worker')")
        conn.exec_driver_sql("""INSERT INTO resume
          (id,owner_userid,expected_cities,expected_job_categories,
           salary_expect_floor_monthly,gender,age,raw_text,audit_status,
           candidate_expires_at,expires_at)
          VALUES (7002,'drift-user',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,
           '男',25,'original-business','pending','2036-02-03 04:05:06',NULL)""")

    def crash(_step, _ordinal, statement):
        if statement == target:
            raise RuntimeError("simulated_ttl_dml_process_death")

    monkeypatch.setattr(runner, "_probe_builds", lambda *_: "b" * 64)
    monkeypatch.setattr(runner, "_after_sql_commit_before_checkpoint", crash)
    with pytest.raises(RuntimeError, match="simulated_ttl_dml_process_death"):
        runner.run_stage(
            isolated_database, command="apply", stage="down",
            probes=["http://isolated.invalid=" + "a" * 40],
            cutover_resume_id=0, confirm_down=True,
        )
    monkeypatch.setattr(runner, "_after_sql_commit_before_checkpoint", lambda *_: None)
    with engine.begin() as conn:
        conn.execute(text("UPDATE resume SET raw_text='unexpected-drift' WHERE id=7002"))
    with pytest.raises(RuntimeError, match="down_export_audit_drift:source_resume"):
        runner.run_stage(
            isolated_database, command="resume", stage="down",
            probes=["http://isolated.invalid=" + "a" * 40],
            cutover_resume_id=0, confirm_down=True,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE system_config SET config_value='0' WHERE config_key='ttl.resume.days'",
        "UPDATE system_config SET config_value='-1' WHERE config_key='ttl.resume.days'",
        "UPDATE system_config SET config_value='abc' WHERE config_key='ttl.resume.days'",
        "UPDATE system_config SET config_value='01' WHERE config_key='ttl.resume.days'",
        "UPDATE system_config SET config_value='3651' WHERE config_key='ttl.resume.days'",
        "UPDATE system_config SET value_type='string' WHERE config_key='ttl.resume.days'",
        "DELETE FROM system_config WHERE config_key='ttl.resume.days'",
        (
            "ALTER TABLE system_config DROP PRIMARY KEY;"
            "INSERT INTO system_config(config_key,config_value,value_type) "
            "VALUES('ttl.resume.days','30','int')"
        ),
    ],
)
def test_down_revalidates_ttl_after_verify_before_any_down_write(
    isolated_database, monkeypatch, mutation,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _prepare_verified_down_state(isolated_database)
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        for statement in mutation.split(";"):
            conn.exec_driver_sql(statement)
    monkeypatch.setattr(runner, "_probe_builds", lambda *_: "b" * 64)
    with pytest.raises(RuntimeError, match="down_ttl_config_invalid"):
        runner.run_stage(
            isolated_database, command="apply", stage="down",
            probes=["http://isolated.invalid=" + "a" * 40],
            cutover_resume_id=0, confirm_down=True,
        )
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT COUNT(*) FROM phase11_migration_ledger
          WHERE migration_key='phase11_resume_lifecycle_down'""")).scalar_one() == 0
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.tables
          WHERE table_schema=DATABASE() AND table_name LIKE 'phase11_resume_down_%'""")).scalar_one() == 0


@pytest.mark.parametrize("mutation", ["ttl", "resume_update", "resume_insert"])
def test_down_capture_write_fence_closes_check_to_dml_and_ddl_window(
    isolated_database, monkeypatch, mutation,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _prepare_verified_down_state(isolated_database)
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO user(external_userid,role) VALUES ('freeze-user','worker')"
        )
        conn.exec_driver_sql("""INSERT INTO resume
          (id,owner_userid,expected_cities,expected_job_categories,
           salary_expect_floor_monthly,gender,age,raw_text,audit_status,
           candidate_expires_at,expires_at)
          VALUES (7101,'freeze-user',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,
           '男',25,'freeze-source','pending',UTC_TIMESTAMP(6)+INTERVAL 7 DAY,NULL)""")
    attempts: list[str] = []

    def after_checkpoint(_step, ordinal, _statement):
        if ordinal != 22 or attempts:
            return
        statement = {
            "ttl": "UPDATE system_config SET config_value='8' WHERE config_key='ttl.resume.days'",
            "resume_update": "UPDATE resume SET raw_text='raced' WHERE id=7101",
            "resume_insert": """INSERT INTO resume
              (id,owner_userid,expected_cities,expected_job_categories,
               salary_expect_floor_monthly,gender,age,raw_text,audit_status,
               candidate_expires_at,expires_at)
              VALUES (7102,'freeze-user',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,
               '男',25,'raced','pending',UTC_TIMESTAMP(6)+INTERVAL 7 DAY,NULL)""",
        }[mutation]
        with pytest.raises(DBAPIError, match="phase11_down_write_frozen"):
            with engine.begin() as racing:
                racing.exec_driver_sql(statement)
        attempts.append(mutation)

    monkeypatch.setattr(runner, "_probe_builds", lambda *_: "b" * 64)
    monkeypatch.setattr(runner, "_after_sql_checkpoint", after_checkpoint)
    runner.run_stage(
        isolated_database, command="apply", stage="down",
        probes=["http://isolated.invalid=" + "a" * 40],
        cutover_resume_id=0, confirm_down=True,
    )
    assert attempts == [mutation]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM resume WHERE id=7102")).scalar_one() == 0
        assert conn.execute(text("SELECT raw_text FROM resume WHERE id=7101")).scalar_one() == "freeze-source"
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.triggers
          WHERE trigger_schema=DATABASE() AND trigger_name LIKE 'phase11_down_freeze_%'""")).scalar_one() == 0


def test_down_freeze_install_failure_releases_lock_and_partial_triggers(
    isolated_database, monkeypatch,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    engine = create_engine(isolated_database)
    # Force the second CREATE TRIGGER to fail after the first one committed.
    # Installation must clean up because _run_sql has not yet set its outer
    # down_write_frozen flag at this point.
    monkeypatch.setattr(
        runner, "_down_freeze_trigger_name", lambda *_: "phase11_down_freeze_collision",
    )
    with engine.connect() as conn:
        with pytest.raises(DBAPIError):
            runner._install_down_write_freeze(conn)
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.triggers
          WHERE trigger_schema=DATABASE()
          AND trigger_name='phase11_down_freeze_collision'""")).scalar_one() == 0
        assert conn.exec_driver_sql(
            "SELECT GET_LOCK(%s,0)", (runner._DOWN_FREEZE_LOCK,),
        ).scalar_one() == 1
        assert conn.exec_driver_sql(
            "SELECT RELEASE_LOCK(%s)", (runner._DOWN_FREEZE_LOCK,),
        ).scalar_one() == 1


def test_down_freeze_runtime_failure_cleans_before_return(
    isolated_database, monkeypatch,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    _prepare_verified_down_state(isolated_database)
    engine = create_engine(isolated_database)

    def fail_after_fence(_step, ordinal, _statement):
        if ordinal == 1:
            raise RuntimeError("simulated_down_runtime_failure")

    monkeypatch.setattr(runner, "_probe_builds", lambda *_: "b" * 64)
    monkeypatch.setattr(runner, "_after_sql_checkpoint", fail_after_fence)
    with pytest.raises(RuntimeError, match="simulated_down_runtime_failure"):
        runner.run_stage(
            isolated_database, command="apply", stage="down",
            probes=["http://isolated.invalid=" + "a" * 40],
            cutover_resume_id=0, confirm_down=True,
        )
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.triggers
          WHERE trigger_schema=DATABASE()
          AND trigger_name LIKE 'phase11_down_freeze_%'""")).scalar_one() == 0
        assert conn.exec_driver_sql(
            "SELECT IS_FREE_LOCK(%s)", (runner._DOWN_FREEZE_LOCK,),
        ).scalar_one() == 1


def test_down_freeze_is_inert_after_owner_connection_is_killed(
    isolated_database,
):
    """A hard-killed runner may leave DDL, but must not freeze the business."""
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    engine = create_engine(isolated_database, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO user(external_userid,role) VALUES ('freeze-crash','worker')"
        )
        conn.exec_driver_sql("""INSERT INTO resume
          (id,owner_userid,expected_cities,expected_job_categories,
           salary_expect_floor_monthly,gender,age,raw_text,audit_status,expires_at)
          VALUES (7111,'freeze-crash',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,
           '男',25,'before-crash','passed',UTC_TIMESTAMP(6)+INTERVAL 30 DAY)""")

    owner = engine.connect()
    runner._install_down_write_freeze(owner)
    owner_id = owner.exec_driver_sql("SELECT CONNECTION_ID()").scalar_one()
    with pytest.raises(DBAPIError, match="phase11_down_write_frozen"):
        with engine.begin() as racing:
            racing.exec_driver_sql(
                "UPDATE resume SET raw_text='blocked' WHERE id=7111"
            )
    with engine.connect() as killer:
        killer.exec_driver_sql(f"KILL CONNECTION {int(owner_id)}")
    try:
        owner.close()
    except DBAPIError:
        pass

    # MySQL releases the named lock with the killed connection.  Durable
    # triggers can remain until resume, but without an owner they are inert.
    with engine.begin() as business:
        assert business.exec_driver_sql(
            "SELECT IS_FREE_LOCK(%s)", (runner._DOWN_FREEZE_LOCK,),
        ).scalar_one() == 1
        business.exec_driver_sql(
            "UPDATE resume SET raw_text='after-crash' WHERE id=7111"
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT raw_text FROM resume WHERE id=7111")).scalar_one() == "after-crash"
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.triggers
          WHERE trigger_schema=DATABASE()
          AND trigger_name LIKE 'phase11_down_freeze_%'""")).scalar_one() > 0
        # A resumed runner removes stale triggers before installing its own
        # fence, and normal release leaves neither lock nor trigger behind.
        runner._install_down_write_freeze(conn)
        runner._release_down_write_freeze(conn)
    with engine.connect() as conn:
        assert conn.execute(text("""SELECT COUNT(*) FROM information_schema.triggers
          WHERE trigger_schema=DATABASE()
          AND trigger_name LIKE 'phase11_down_freeze_%'""")).scalar_one() == 0
        assert conn.exec_driver_sql(
            "SELECT IS_FREE_LOCK(%s)", (runner._DOWN_FREEZE_LOCK,),
        ).scalar_one() == 1


@pytest.mark.parametrize(
    "raw,value_type", [("01", "int"), ("+1", "int"), (" 7", "int"),
                       ("7 ", "int"), ("７", "int"), ("7", "string"),
                       ("0", "int"), ("366", "int")],
)
def test_lifecycle_backfill_invalid_candidate_ttl_is_read_only(
    isolated_database, raw, value_type,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO user(external_userid,role) VALUES ('ttl-invalid','worker')")
        conn.execute(text("""INSERT INTO system_config
          (config_key,config_value,value_type) VALUES
          ('ttl.resume.candidate.days',:raw,:value_type)"""), {
              "raw": raw, "value_type": value_type,
          })
        conn.exec_driver_sql("""INSERT INTO resume
          (id,owner_userid,expected_cities,expected_job_categories,
           salary_expect_floor_monthly,gender,age,raw_text,audit_status,expires_at)
          VALUES (7201,'ttl-invalid',JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,
           '男',25,'ttl-invalid','pending',UTC_TIMESTAMP(6)+INTERVAL 30 DAY)""")
        conn.execute(text("""INSERT INTO phase11_migration_ledger
          (migration_key,script_sha256,stage,kind,status,attempt,last_statement_ordinal,
           started_at,cutover_resume_id,executed_by)
          VALUES (:key,:sha,'post_cutover','python','running',1,0,
           UTC_TIMESTAMP(6),7201,'test')"""), {
              "key": lifecycle_backfill.MIGRATION_KEY, "sha": "a" * 64,
          })
    with pytest.raises(RuntimeError, match="candidate_ttl_config_invalid"):
        backfill(isolated_database, apply=True, batch_size=1, cursor=0)
    with engine.connect() as conn:
        row = conn.execute(text("""SELECT expires_at,candidate_expires_at,activated_at
          FROM resume WHERE id=7201""")).one()
        cursor = conn.execute(text("""SELECT resume_cursor_json FROM phase11_migration_ledger
          WHERE migration_key=:key"""), {"key": lifecycle_backfill.MIGRATION_KEY}).scalar_one()
    assert row.expires_at is not None and row.candidate_expires_at is None and row.activated_at is None
    assert cursor in (None, {}, "{}")


def test_lifecycle_backfill_pins_ttl_revision_and_fails_closed_between_batches(
    isolated_database, monkeypatch,
):
    _load_sql(isolated_database, BACKEND / "sql" / "schema.sql")
    engine = create_engine(isolated_database)
    with engine.begin() as conn:
        conn.exec_driver_sql("""INSERT INTO user(external_userid,role) VALUES
          ('ttl-drift-1','worker'),('ttl-drift-2','worker')""")
        conn.exec_driver_sql("""INSERT INTO system_config
          (config_key,config_value,value_type,updated_by) VALUES
          ('ttl.resume.candidate.days','7','int','initial')""")
        for resume_id in (7301, 7302):
            conn.execute(text("""INSERT INTO resume
              (id,owner_userid,expected_cities,expected_job_categories,
               salary_expect_floor_monthly,gender,age,raw_text,audit_status,expires_at)
              VALUES (:id,:owner,JSON_ARRAY('上海'),JSON_ARRAY('普工'),5000,
               '男',25,:raw,'pending',UTC_TIMESTAMP(6)+INTERVAL 30 DAY)"""), {
                   "id": resume_id, "owner": f"ttl-drift-{resume_id - 7300}",
                   "raw": f"ttl-drift-{resume_id}",
               })
        conn.execute(text("""INSERT INTO phase11_migration_ledger
          (migration_key,script_sha256,stage,kind,status,attempt,last_statement_ordinal,
           started_at,cutover_resume_id,executed_by)
          VALUES (:key,:sha,'post_cutover','python','running',1,0,
           UTC_TIMESTAMP(6),7302,'test')"""), {
              "key": lifecycle_backfill.MIGRATION_KEY, "sha": "a" * 64,
          })
    changed = False

    def change_after_first_checkpoint():
        nonlocal changed
        if changed:
            return
        with engine.begin() as conn:
            conn.exec_driver_sql("""UPDATE system_config SET config_value='8',updated_by='racer'
              WHERE config_key='ttl.resume.candidate.days'""")
        changed = True

    monkeypatch.setattr(lifecycle_backfill, "_after_checkpoint", change_after_first_checkpoint)
    with pytest.raises(RuntimeError, match="candidate_ttl_config_drift"):
        backfill(isolated_database, apply=True, batch_size=1, cursor=0)
    with engine.connect() as conn:
        rows = conn.execute(text("""SELECT id,expires_at,candidate_expires_at
          FROM resume WHERE id IN (7301,7302) ORDER BY id""")).all()
        cursor = conn.execute(text("""SELECT resume_cursor_json FROM phase11_migration_ledger
          WHERE migration_key=:key"""), {"key": lifecycle_backfill.MIGRATION_KEY}).scalar_one()
    if isinstance(cursor, str):
        cursor = json.loads(cursor)
    evidence = cursor["candidate_ttl_evidence"]
    assert evidence["config_value"] == "7" and evidence["value_type"] == "int"
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["revision"])
    assert rows[0].expires_at is None and rows[0].candidate_expires_at is not None
    assert rows[1].expires_at is not None and rows[1].candidate_expires_at is None
