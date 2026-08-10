"""Phase 10 preflight checks that require a real MySQL database."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from scripts.phase10_preflight import CHECKS, collect_redis_policy


pytestmark = pytest.mark.integration


def test_session_schema_and_live_redis_rollout_gates_pass():
    db = SessionLocal()
    try:
        for name in (
            "session_commit_deadline_schema_mismatch",
            "session_apply_lease_owner_schema_mismatch",
            "session_commit_due_index_mismatch",
        ):
            assert int(db.execute(text(CHECKS[name])).scalar_one()) == 0, name
    finally:
        db.close()

    redis_policy = collect_redis_policy()
    assert redis_policy == {
        "redis_durability_policy_mismatch": 0,
        "redis_maxmemory_policy": "noeviction",
        "redis_appendonly": "yes",
        "redis_appendfsync": "always",
    }


def test_job_ttl_preflight_accepts_full_supported_range():
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO system_config "
                "(config_key, config_value, value_type, description) "
                "VALUES ('ttl.job.days', '30', 'int', 'integration test') "
                "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)"
            )
        )
        for value, expected in (
            ("365", 0),
            ("366", 0),
            ("3650", 0),
            ("0", 1),
            ("3651", 1),
            ("invalid", 1),
        ):
            db.execute(
                text(
                    "UPDATE system_config SET config_value=:value "
                    "WHERE config_key='ttl.job.days'"
                ),
                {"value": value},
            )
            result = db.execute(text(CHECKS["invalid_job_ttl_config"])).scalar_one()
            assert int(result) == expected, value
    finally:
        db.rollback()
        db.close()
