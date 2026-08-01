"""Pre-launch compatibility preflight for recommendation-v1 (§5.4.1 / §14.1).

`match.max_candidates` and `match.top_n` stay **legacy-only** parameters: v1 is
hard-wired to 50/20/3 and must never read them, while off/shadow control traffic
must keep reproducing whatever historical value production carries.  Nothing in
this project is allowed to rewrite those two keys, so the release procedure
(§11.1.1) inserts a manual step -- "记录 legacy max_candidates/top_n 实际配置" --
between the backup drill and the migrations.  This script is that step.

It reads the production values, validates them, records value + `updated_at` +
SHA256 into `recommendation_preflight_ledger`, and automates every §14.1
compatibility precondition that can be observed from the database and Redis.

It is strictly read-only against business data: the only row it ever writes is
its own ledger row, and only under `--record`.

Standard commands::

    cd backend
    python scripts/recommendation_preflight.py --dsn-env DB_URL --check
    python scripts/recommendation_preflight.py --dsn-env DB_URL --record \
        --release-tag rec-v1-2026-07-26
    python scripts/recommendation_preflight.py --dsn-env DB_URL --check \
        --stage post-migration

`--stage` follows the §11.1.1 ordering: the first preflight runs *before* the
phase9 SQL, so the v1 tables are legitimately absent then; the second one runs
after `--verify` and does require them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

# `python scripts/recommendation_preflight.py` puts `backend/scripts` on sys.path,
# not `backend`.  The `app` imports below are deliberately lazy (an unimportable
# service must surface as a finding, not as a crash), but they still need the
# package to be reachable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# §5.4.1: above this the legacy SQL cost has to be evaluated on its own; this
# project must not silently truncate a historical production value.
MAX_CANDIDATES_SAFETY_LIMIT = 200

LEGACY_CONFIG_KEYS = ("match.max_candidates", "match.top_n")

# Fallbacks in `search_service._get_config_int` when the row is missing or holds
# a non-integer.  Kept here so the report can name the value legacy *actually*
# uses rather than the value someone believes is configured.
LEGACY_CONFIG_CODE_DEFAULTS = {"match.max_candidates": 50, "match.top_n": 3}

EXPECTED_V1_CONSTANTS = {
    "V1_MAX_CANDIDATES": 50,
    "V1_PRECISION_POOL_SIZE": 20,
    "V1_DISPLAY_TOP_N": 3,
}

DIRECTIONS = ("search_job", "search_worker")

MIN_MYSQL_VERSION = (8, 0)

# Present only after phase9; `--stage pre-migration` therefore treats their
# absence as expected instead of as a failure.
PHASE9_TABLES = (
    "recommendation_strategy_version",
    "recommendation_strategy_release",
    "recommendation_release_history",
    "recommendation_runtime_control",
    "recommendation_request",
    "recommendation_search_attempt",
    "recommendation_delivery",
    "recommendation_impression",
    "recommendation_exposure_daily",
)

PHASE9_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "migrations"
    / "phase9_manifest.sha256"
)

PHASE9_REQUIRED_COLUMNS = (
    ("recommendation_delivery", "content_expires_at"),
    ("event_log", "client_event_id"),
    ("event_log", "attribution_dedupe_key"),
    ("recommendation_request", "parent_request_id"),
    ("recommendation_request", "shadow_status"),
)

PHASE9_REQUIRED_INDEXES = (
    ("event_log", "uk_event_client_idempotency"),
    (
        "recommendation_delivery",
        "idx_recommendation_delivery_impression_lease",
    ),
)

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS recommendation_preflight_ledger (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  release_tag VARCHAR(64) NOT NULL,
  stage VARCHAR(32) NOT NULL,
  direction VARCHAR(32) NOT NULL,
  execution_mode VARCHAR(16) NOT NULL,
  legacy_max_candidates INT NOT NULL,
  legacy_top_n INT NOT NULL,
  legacy_max_candidates_updated_at DATETIME(6) NULL,
  legacy_top_n_updated_at DATETIME(6) NULL,
  legacy_config_source VARCHAR(32) NOT NULL,
  v1_max_candidates INT NOT NULL,
  v1_top_n INT NOT NULL,
  sql_fetch_size INT NOT NULL,
  config_sha256 CHAR(64) NOT NULL,
  report_sha256 CHAR(64) NOT NULL,
  success TINYINT(1) NOT NULL,
  findings JSON NULL,
  server_version VARCHAR(128) NULL,
  checked_by VARCHAR(128) NOT NULL,
  checked_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY uk_recommendation_preflight_tag_direction (release_tag, stage, direction),
  KEY idx_recommendation_preflight_checked_at (checked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Re-running the same tag/stage overwrites: a preflight is a statement about the
# state *now*, and keeping a stale failed row next to a fresh passing one would
# make the ledger unreadable.  History of earlier attempts lives in the
# operator's console output and in distinct `--release-tag` values.
LEDGER_UPSERT = (
    "INSERT INTO recommendation_preflight_ledger ("
    "release_tag, stage, direction, execution_mode, "
    "legacy_max_candidates, legacy_top_n, "
    "legacy_max_candidates_updated_at, legacy_top_n_updated_at, "
    "legacy_config_source, v1_max_candidates, v1_top_n, sql_fetch_size, "
    "config_sha256, report_sha256, success, findings, server_version, "
    "checked_by, checked_at) VALUES ("
    ":release_tag, :stage, :direction, :execution_mode, "
    ":legacy_max_candidates, :legacy_top_n, "
    ":legacy_max_candidates_updated_at, :legacy_top_n_updated_at, "
    ":legacy_config_source, :v1_max_candidates, :v1_top_n, :sql_fetch_size, "
    ":config_sha256, :report_sha256, :success, :findings, :server_version, "
    ":checked_by, CURRENT_TIMESTAMP(6)) "
    # Named parameters rather than `VALUES(col)`, which MySQL 8.0.20 deprecated.
    "ON DUPLICATE KEY UPDATE "
    "execution_mode = :execution_mode, "
    "legacy_max_candidates = :legacy_max_candidates, "
    "legacy_top_n = :legacy_top_n, "
    "legacy_max_candidates_updated_at = :legacy_max_candidates_updated_at, "
    "legacy_top_n_updated_at = :legacy_top_n_updated_at, "
    "legacy_config_source = :legacy_config_source, "
    "v1_max_candidates = :v1_max_candidates, "
    "v1_top_n = :v1_top_n, "
    "sql_fetch_size = :sql_fetch_size, "
    "config_sha256 = :config_sha256, "
    "report_sha256 = :report_sha256, "
    "success = :success, "
    "findings = :findings, "
    "server_version = :server_version, "
    "checked_by = :checked_by, "
    "checked_at = CURRENT_TIMESTAMP(6)"
)


# ---------------------------------------------------------------------------
# report primitives
# ---------------------------------------------------------------------------

ERROR = "error"
WARN = "warn"
INFO = "info"


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def dsn_from_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing DSN environment variable: {name}")
    return value


def checked_by() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_server(conn, findings: list[Finding]) -> str | None:
    """MySQL 8.0+ only, same contract as `apply_phase9_migrations.py`."""
    raw = str(conn.execute(text("SELECT VERSION()")).scalar() or "")
    comment = conn.execute(text("SELECT @@version_comment")).scalar()
    fingerprint = f"{raw} / {comment or ''}"
    if not raw:
        findings.append(Finding(ERROR, "server_version_unknown", "SELECT VERSION() returned nothing"))
        return None
    if "mariadb" in fingerprint.lower():
        findings.append(Finding(
            ERROR, "server_unsupported",
            f"recommendation-v1 targets MySQL 8.0 only, server reports MariaDB ({fingerprint})",
        ))
        return raw[:128]
    parts = raw.split("-", 1)[0].split(".")
    try:
        parsed = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        findings.append(Finding(ERROR, "server_version_unparsable", f"unable to parse server version: {fingerprint}"))
        return raw[:128]
    if parsed < MIN_MYSQL_VERSION:
        findings.append(Finding(
            ERROR, "server_too_old",
            f"MySQL {MIN_MYSQL_VERSION[0]}.{MIN_MYSQL_VERSION[1]}+ required, got {fingerprint}",
        ))
    else:
        findings.append(Finding(INFO, "server_version_ok", f"server version {raw}"))
    return raw[:128]


def read_legacy_config(conn, findings: list[Finding]) -> dict[str, dict[str, Any]]:
    """Read -- never write -- the two legacy keys (§5.4.1).

    `search_service._get_config_int` silently falls back to 50/3 when the row is
    missing or unparsable, so a corrupted value does not surface as an error at
    runtime; it just makes production behave differently from what the config
    table shows.  Preflight is the place where that divergence must be loud.
    """
    rows = conn.execute(text(
        "SELECT config_key, config_value, value_type, updated_at, updated_by "
        "FROM system_config WHERE config_key IN (:a, :b)"
    ), {"a": LEGACY_CONFIG_KEYS[0], "b": LEGACY_CONFIG_KEYS[1]})
    stored = {str(row[0]): row for row in rows}

    result: dict[str, dict[str, Any]] = {}
    for key in LEGACY_CONFIG_KEYS:
        row = stored.get(key)
        if row is None:
            findings.append(Finding(
                WARN, "legacy_config_missing",
                f"{key} has no system_config row; legacy runs on the code default "
                f"{LEGACY_CONFIG_CODE_DEFAULTS[key]}",
                {"config_key": key},
            ))
            result[key] = {
                "config_key": key,
                "raw_value": None,
                "value_type": None,
                "updated_at": None,
                "updated_by": None,
                "effective_value": LEGACY_CONFIG_CODE_DEFAULTS[key],
                "source": "code_default",
            }
            continue

        raw_value = row[1]
        entry = {
            "config_key": key,
            "raw_value": None if raw_value is None else str(raw_value),
            "value_type": None if row[2] is None else str(row[2]),
            "updated_at": iso(row[3]),
            "updated_by": None if row[4] is None else str(row[4]),
        }
        try:
            parsed = int(str(raw_value))
        except (TypeError, ValueError):
            findings.append(Finding(
                ERROR, "legacy_config_not_integer",
                f"{key}={raw_value!r} is not an integer; legacy silently falls back to "
                f"{LEGACY_CONFIG_CODE_DEFAULTS[key]}, so the configured value is a lie",
                {"config_key": key, "raw_value": entry["raw_value"]},
            ))
            entry["effective_value"] = LEGACY_CONFIG_CODE_DEFAULTS[key]
            entry["source"] = "code_default"
            result[key] = entry
            continue

        if parsed <= 0:
            findings.append(Finding(
                ERROR, "legacy_config_not_positive",
                f"{key}={parsed} must be a positive integer",
                {"config_key": key, "value": parsed},
            ))
        entry["effective_value"] = parsed
        entry["source"] = "system_config"
        result[key] = entry
    return result


def check_legacy_limits(config: dict[str, dict[str, Any]], findings: list[Finding]) -> None:
    """`max_candidates >= top_n` plus the §5.4.1 safety ceiling."""
    max_candidates = int(config["match.max_candidates"]["effective_value"])
    top_n = int(config["match.top_n"]["effective_value"])

    if max_candidates < top_n:
        findings.append(Finding(
            ERROR, "legacy_limits_inverted",
            f"match.max_candidates ({max_candidates}) must be >= match.top_n ({top_n})",
            {"max_candidates": max_candidates, "top_n": top_n},
        ))

    if max_candidates > MAX_CANDIDATES_SAFETY_LIMIT:
        findings.append(Finding(
            ERROR, "legacy_max_candidates_above_safety_limit",
            f"match.max_candidates={max_candidates} 超过安全上限 {MAX_CANDIDATES_SAFETY_LIMIT}："
            "必须先单独评估现有 legacy 性能，不能由本项目静默截断。"
            "preflight 不会修改该配置，请走数据库变更单 + 审计 + legacy 回归。",
            {"max_candidates": max_candidates, "safety_limit": MAX_CANDIDATES_SAFETY_LIMIT},
        ))

    if top_n != EXPECTED_V1_CONSTANTS["V1_DISPLAY_TOP_N"]:
        # Not a failure -- §5.4.1 explicitly allows any legal historical value and
        # forbids normalising it.  It is recorded so the ledger shows up front that
        # v1 and legacy will serve different page sizes after launch.
        findings.append(Finding(
            INFO, "legacy_top_n_differs_from_v1",
            f"match.top_n={top_n} 与 v1 固定的 3 不同；legacy/对照组保持 {top_n}，v1 保持 3，"
            "preflight 不得自动改成 50/3",
            {"legacy_top_n": top_n, "v1_top_n": EXPECTED_V1_CONSTANTS["V1_DISPLAY_TOP_N"]},
        ))


def check_v1_constants(findings: list[Finding]) -> dict[str, Any]:
    """§14.1: v1 stays 50/20/3 for any legal historical general config."""
    try:
        from app.services import recommendation_scoring_service as scoring
    except Exception as exc:  # pragma: no cover - import failure is itself the finding
        findings.append(Finding(
            ERROR, "v1_constants_unreadable",
            f"cannot import recommendation_scoring_service: {exc.__class__.__name__}: {exc}",
        ))
        return {}
    actual = {name: getattr(scoring, name, None) for name in EXPECTED_V1_CONSTANTS}
    for name, expected in EXPECTED_V1_CONSTANTS.items():
        if actual[name] != expected:
            findings.append(Finding(
                ERROR, "v1_constant_mismatch",
                f"{name}={actual[name]!r}, expected {expected}",
                {"constant": name, "expected": expected, "actual": actual[name]},
            ))
    if all(actual[name] == expected for name, expected in EXPECTED_V1_CONSTANTS.items()):
        findings.append(Finding(INFO, "v1_constants_ok", "v1 constants are 50/20/3"))
    return actual


def check_config_lock(findings: list[Finding]) -> None:
    """§14.1: both keys hidden in the general admin UI and locked for writes."""
    try:
        from app.services import system_config_service
    except Exception as exc:  # pragma: no cover
        findings.append(Finding(
            ERROR, "config_lock_unreadable",
            f"cannot import system_config_service: {exc.__class__.__name__}: {exc}",
        ))
        return
    locked = set(getattr(system_config_service, "LOCKED_RECOMMENDATION_KEYS", set()))
    hidden = set(getattr(system_config_service, "_HIDDEN_KEYS", set()))
    missing_lock = sorted(set(LEGACY_CONFIG_KEYS) - locked)
    missing_hidden = sorted(set(LEGACY_CONFIG_KEYS) - hidden)
    if missing_lock:
        findings.append(Finding(
            ERROR, "config_not_locked",
            f"update API does not reject these keys: {missing_lock}",
            {"keys": missing_lock},
        ))
    if missing_hidden:
        findings.append(Finding(
            ERROR, "config_not_hidden",
            f"general admin config list still exposes: {missing_hidden}",
            {"keys": missing_hidden},
        ))
    if not missing_lock and not missing_hidden:
        findings.append(Finding(
            INFO, "config_lock_ok",
            "match.max_candidates/match.top_n are hidden and return config_locked_by_recommendation_v1",
        ))


def existing_tables(conn) -> set[str]:
    return {
        str(row[0]) for row in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
        ))
    }


def _phase9_manifest_entries() -> tuple[dict[str, str], list[str]]:
    expected: dict[str, str] = {}
    file_mismatches: list[str] = []
    root = PHASE9_MANIFEST.parent
    for raw_line in PHASE9_MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        digest, filename = line.split(None, 1)
        filename = filename.strip()
        expected[filename] = digest
        path = root / filename
        actual = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.exists() else "missing"
        )
        if actual != digest:
            file_mismatches.append(filename)
    return expected, file_mismatches


def _check_phase9_structure(conn, findings: list[Finding]) -> None:
    columns = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        ))
    }
    indexes = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(text(
            "SELECT TABLE_NAME, INDEX_NAME "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        ))
    }
    parent_indexed = bool(conn.execute(text(
        "SELECT 1 FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'recommendation_request' "
        "AND COLUMN_NAME = 'parent_request_id' "
        "AND SEQ_IN_INDEX = 1 LIMIT 1"
    )).first())
    missing_columns = [
        f"{table}.{column}"
        for table, column in PHASE9_REQUIRED_COLUMNS
        if (table, column) not in columns
    ]
    missing_indexes = [
        f"{table}.{index}"
        for table, index in PHASE9_REQUIRED_INDEXES
        if (table, index) not in indexes
    ]
    if not parent_indexed:
        missing_indexes.append(
            "recommendation_request(parent_request_id)",
        )
    if missing_columns or missing_indexes:
        findings.append(Finding(
            ERROR,
            "phase9_structure_incomplete",
            "critical Phase 9 columns/indexes are missing",
            {
                "missing_columns": missing_columns,
                "missing_indexes": missing_indexes,
            },
        ))
        return

    ttl_missing = int(conn.execute(text(
        "SELECT COUNT(*) FROM recommendation_delivery "
        "WHERE content_expires_at IS NULL "
        "AND (content_ciphertext IS NOT NULL "
        "OR session_patch_ciphertext IS NOT NULL)"
    )).scalar() or 0)
    if ttl_missing:
        findings.append(Finding(
            ERROR,
            "recommendation_content_ttl_missing",
            f"{ttl_missing} encrypted delivery rows have no content expiry",
            {"rows": ttl_missing},
        ))
    else:
        findings.append(Finding(
            INFO,
            "phase9_structure_ok",
            "critical Phase 9 columns, indexes and encrypted-content TTLs are present",
        ))


def check_phase9_applied(conn, tables: set[str], stage: str, findings: list[Finding]) -> None:
    missing = [name for name in PHASE9_TABLES if name not in tables]
    if stage == "pre-migration":
        findings.append(Finding(
            INFO, "phase9_not_required_yet",
            f"stage=pre-migration; {len(missing)}/{len(PHASE9_TABLES)} v1 tables absent, as expected",
            {"missing": missing},
        ))
        return
    if missing:
        findings.append(Finding(
            ERROR, "phase9_tables_missing",
            f"stage=post-migration but these tables do not exist: {missing}; "
            "run apply_phase9_migrations.py --apply and --verify first",
            {"missing": missing},
        ))
        return
    if "schema_migration_history" not in tables:
        findings.append(Finding(ERROR, "migration_ledger_missing", "schema_migration_history does not exist"))
        return
    expected, file_mismatches = _phase9_manifest_entries()
    if file_mismatches:
        findings.append(Finding(
            ERROR,
            "phase9_manifest_checksum_invalid",
            f"local Phase 9 migration files do not match manifest: {file_mismatches}",
            {"migrations": file_mismatches},
        ))
        return
    rows = list(conn.execute(text(
        "SELECT migration_name, sha256, success FROM schema_migration_history "
        "WHERE migration_name LIKE 'phase9%'"
    )))
    recorded = {
        str(row[0]): (str(row[1]), int(row[2]))
        for row in rows
    }
    failed = sorted(
        name for name, (_digest, success) in recorded.items()
        if success != 1
    )
    missing = sorted(set(expected) - set(recorded))
    stale = sorted(
        name for name, digest in expected.items()
        if name in recorded and recorded[name][0] != digest
    )
    if not rows:
        findings.append(Finding(ERROR, "migration_ledger_empty", "no phase9 rows in schema_migration_history"))
    elif failed:
        findings.append(Finding(
            ERROR, "migration_ledger_failed",
            f"phase9 migrations recorded success=0: {failed}",
            {"migrations": failed},
        ))
    elif missing or stale:
        findings.append(Finding(
            ERROR,
            "migration_ledger_incomplete",
            "Phase 9 ledger is missing manifest entries or has stale checksums",
            {
                "missing": missing,
                "stale_checksum": stale,
            },
        ))
    else:
        findings.append(Finding(
            INFO, "phase9_applied",
            f"{len(expected)} manifest migrations applied with matching checksums",
        ))
        _check_phase9_structure(conn, findings)


def read_release_state(conn, tables: set[str], stage: str, findings: list[Finding]) -> dict[str, dict[str, Any]]:
    """§14.1: deploy with kill/off, and never point a release at a missing version."""
    state = {direction: {"execution_mode": "off", "known": False} for direction in DIRECTIONS}
    if "recommendation_strategy_release" not in tables:
        if stage == "post-migration":
            findings.append(Finding(ERROR, "release_table_missing", "recommendation_strategy_release does not exist"))
        else:
            findings.append(Finding(
                INFO, "release_state_unknown",
                "release table not created yet; both directions treated as off (§14.1 empty-table fallback)",
            ))
        return state

    rows = list(conn.execute(text(
        "SELECT direction, execution_mode, stable_version_id, candidate_version_id, "
        "rollout_percentage, revision FROM recommendation_strategy_release"
    )))
    known_versions = {
        int(row[0]) for row in conn.execute(text(
            "SELECT id FROM recommendation_strategy_version"
        ))
    } if "recommendation_strategy_version" in tables else set()

    seen = set()
    for row in rows:
        direction = str(row[0])
        seen.add(direction)
        entry = {
            "execution_mode": str(row[1]),
            "stable_version_id": None if row[2] is None else int(row[2]),
            "candidate_version_id": None if row[3] is None else int(row[3]),
            "rollout_percentage": int(row[4]),
            "revision": int(row[5]),
            "known": True,
        }
        if direction in state:
            state[direction] = entry
        if entry["execution_mode"] != "off":
            findings.append(Finding(
                WARN, "direction_not_off",
                f"{direction} execution_mode={entry['execution_mode']}; §11.1.1 requires deploying "
                "with kill/off and only then moving to shadow",
                {"direction": direction, "execution_mode": entry["execution_mode"]},
            ))
        for column in ("stable_version_id", "candidate_version_id"):
            version_id = entry[column]
            if version_id is not None and known_versions and version_id not in known_versions:
                findings.append(Finding(
                    ERROR, "release_points_at_missing_version",
                    f"{direction}.{column}={version_id} has no recommendation_strategy_version row",
                    {"direction": direction, "column": column, "version_id": version_id},
                ))

    for direction in DIRECTIONS:
        if direction not in seen:
            level = ERROR if stage == "post-migration" else INFO
            findings.append(Finding(
                level, "release_row_missing",
                f"no recommendation_strategy_release row for {direction}; "
                "the empty-table fallback keeps it on legacy",
                {"direction": direction},
            ))
    return state


def check_kill_switch(conn, tables: set[str], stage: str, findings: list[Finding]) -> dict[str, Any]:
    if "recommendation_runtime_control" not in tables:
        if stage == "post-migration":
            findings.append(Finding(
                ERROR, "runtime_control_table_missing",
                "recommendation_runtime_control does not exist; the emergency kill switch has no home",
            ))
        return {"scope": "global", "kill_switch": None, "known": False}
    row = conn.execute(text(
        "SELECT kill_switch, revision FROM recommendation_runtime_control WHERE scope = 'global'"
    )).first()
    if row is None:
        level = ERROR if stage == "post-migration" else INFO
        findings.append(Finding(
            level, "kill_switch_row_missing",
            "no recommendation_runtime_control row for scope=global; "
            "call recommendation_strategy_service.ensure_initial_release before enabling shadow",
        ))
        return {"scope": "global", "kill_switch": None, "known": False}
    engaged = bool(int(row[0]))
    findings.append(Finding(
        INFO, "kill_switch_state",
        f"global kill_switch={'on' if engaged else 'off'} (revision={int(row[1])})",
        {"kill_switch": engaged, "revision": int(row[1])},
    ))
    return {"scope": "global", "kill_switch": engaged, "revision": int(row[1]), "known": True}


def check_redis_compat(sample_size: int, findings: list[Finding]) -> dict[str, Any]:
    """§14.1: old Redis sessions deserialize, old send-retry payloads still consume."""
    summary: dict[str, Any] = {"checked": False}
    try:
        from app.core.redis_client import QUEUE_SEND_RETRY, SESSION_PREFIX, get_redis
        from app.schemas.conversation import SessionState
    except Exception as exc:
        findings.append(Finding(
            ERROR, "redis_compat_unreadable",
            f"cannot import the Redis/session modules: {exc.__class__.__name__}: {exc}",
        ))
        return summary

    try:
        client = get_redis()
        client.ping()
    except Exception as exc:
        findings.append(Finding(
            ERROR, "redis_unreachable",
            f"cannot reach Redis: {exc.__class__.__name__}: {exc}; rerun with --skip-redis "
            "only if the session/send-retry compatibility was verified another way",
        ))
        return summary

    session_keys: list[str] = []
    try:
        for key in client.scan_iter(match=f"{SESSION_PREFIX}*", count=200):
            session_keys.append(key if isinstance(key, str) else key.decode("utf-8"))
            if len(session_keys) >= sample_size:
                break
    except Exception as exc:
        findings.append(Finding(ERROR, "redis_scan_failed", f"SCAN failed: {exc.__class__.__name__}: {exc}"))
        return summary

    broken: list[str] = []
    snapshot_algorithms: dict[str, int] = {}
    for key in session_keys:
        try:
            raw = client.get(key)
            if raw is None:
                continue
            payload = json.loads(raw)
            session = SessionState(**payload)
        except Exception as exc:
            # The key itself is a user identifier, so only the failure class is
            # reported; the operator can locate it from Redis directly.
            broken.append(f"{exc.__class__.__name__}: {exc}")
            continue
        snapshot = session.candidate_snapshot
        algorithm = snapshot.algorithm_version if snapshot is not None else "none"
        snapshot_algorithms[algorithm] = snapshot_algorithms.get(algorithm, 0) + 1

    if broken:
        findings.append(Finding(
            ERROR, "legacy_session_incompatible",
            f"{len(broken)}/{len(session_keys)} sampled Redis sessions failed to deserialize into "
            f"SessionState: {sorted(set(broken))[:3]}",
            {"failed": len(broken), "sampled": len(session_keys)},
        ))
    elif session_keys:
        findings.append(Finding(
            INFO, "legacy_session_ok",
            f"{len(session_keys)} sampled Redis sessions deserialize cleanly; "
            f"snapshot algorithm_version distribution {snapshot_algorithms}",
            {"sampled": len(session_keys), "snapshot_algorithms": snapshot_algorithms},
        ))
    else:
        findings.append(Finding(INFO, "legacy_session_no_sample", "no live Redis sessions to sample"))

    retry_broken: list[str] = []
    retry_sampled = 0
    try:
        entries = client.lrange(QUEUE_SEND_RETRY, 0, max(sample_size - 1, 0))
    except Exception as exc:
        findings.append(Finding(
            ERROR, "send_retry_unreadable",
            f"cannot read {QUEUE_SEND_RETRY}: {exc.__class__.__name__}: {exc}",
        ))
        entries = []
    for entry in entries:
        retry_sampled += 1
        try:
            payload = json.loads(entry)
            if not payload.get("userid") or not payload.get("content"):
                raise ValueError("missing userid/content")
            int(payload.get("send_retry_count") or 0)
            float(payload.get("backoff_until") or 0)
        except Exception as exc:
            retry_broken.append(f"{exc.__class__.__name__}: {exc}")

    if retry_broken:
        findings.append(Finding(
            ERROR, "send_retry_incompatible",
            f"{len(retry_broken)}/{retry_sampled} queued send-retry payloads are not consumable: "
            f"{sorted(set(retry_broken))[:3]}",
            {"failed": len(retry_broken), "sampled": retry_sampled},
        ))
    elif retry_sampled:
        findings.append(Finding(
            INFO, "send_retry_ok",
            f"{retry_sampled} queued send-retry payloads are consumable",
            {"sampled": retry_sampled},
        ))
    else:
        findings.append(Finding(INFO, "send_retry_no_sample", "send-retry queue is empty"))

    summary = {
        "checked": True,
        "sessions_sampled": len(session_keys),
        "sessions_failed": len(broken),
        "snapshot_algorithms": snapshot_algorithms,
        "send_retry_sampled": retry_sampled,
        "send_retry_failed": len(retry_broken),
    }
    return summary


# ---------------------------------------------------------------------------
# report assembly
# ---------------------------------------------------------------------------

def direction_rows(
    config: dict[str, dict[str, Any]],
    release_state: dict[str, dict[str, Any]],
    v1_constants: dict[str, Any],
) -> list[dict[str, Any]]:
    """The §5.4.1 table, resolved against the values production really carries.

    `sql_fetch_size` is the widest candidate pull the direction can issue: off
    never exceeds the legacy value, while shadow (and the v1 arm of on) needs
    `max(legacy, 50)` so both sides can slice the same result set without
    changing legacy's `created_at DESC,id DESC` order.
    """
    legacy_max = int(config["match.max_candidates"]["effective_value"])
    legacy_top_n = int(config["match.top_n"]["effective_value"])
    v1_max = int(v1_constants.get("V1_MAX_CANDIDATES") or EXPECTED_V1_CONSTANTS["V1_MAX_CANDIDATES"])
    v1_top_n = int(v1_constants.get("V1_DISPLAY_TOP_N") or EXPECTED_V1_CONSTANTS["V1_DISPLAY_TOP_N"])

    rows = []
    for direction in DIRECTIONS:
        mode = str(release_state.get(direction, {}).get("execution_mode") or "off")
        sql_fetch_size = legacy_max if mode == "off" else max(legacy_max, v1_max)
        rows.append({
            "direction": direction,
            "execution_mode": mode,
            "legacy_max_candidates": legacy_max,
            "legacy_top_n": legacy_top_n,
            "legacy_max_candidates_updated_at": config["match.max_candidates"]["updated_at"],
            "legacy_top_n_updated_at": config["match.top_n"]["updated_at"],
            "legacy_config_source": (
                "system_config"
                if config["match.max_candidates"]["source"] == "system_config"
                and config["match.top_n"]["source"] == "system_config"
                else "code_default"
            ),
            "v1_max_candidates": v1_max,
            "v1_top_n": v1_top_n,
            "sql_fetch_size": sql_fetch_size,
        })
    return rows


def build_report(args, conn) -> dict[str, Any]:
    findings: list[Finding] = []
    server_version = check_server(conn, findings)
    config = read_legacy_config(conn, findings)
    check_legacy_limits(config, findings)
    v1_constants = check_v1_constants(findings)
    check_config_lock(findings)

    tables = existing_tables(conn)
    check_phase9_applied(conn, tables, args.stage, findings)
    release_state = read_release_state(conn, tables, args.stage, findings)
    kill_switch = check_kill_switch(conn, tables, args.stage, findings)

    if args.skip_redis:
        findings.append(Finding(
            WARN, "redis_checks_skipped",
            "--skip-redis: §14.1 old-session / old send-retry compatibility was not verified",
        ))
        redis_summary = {"checked": False}
    else:
        redis_summary = check_redis_compat(args.sample_size, findings)

    if args.strict:
        findings = [
            Finding(ERROR, item.code, item.message, item.detail) if item.level == WARN else item
            for item in findings
        ]

    config_sha256 = sha256_of({
        key: {
            "raw_value": entry["raw_value"],
            "value_type": entry["value_type"],
            "updated_at": entry["updated_at"],
            "updated_by": entry["updated_by"],
            "effective_value": entry["effective_value"],
            "source": entry["source"],
        }
        for key, entry in config.items()
    })

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_tag": args.release_tag,
        "stage": args.stage,
        "checked_by": checked_by(),
        "server_version": server_version,
        "safety_limit": MAX_CANDIDATES_SAFETY_LIMIT,
        "legacy_config": config,
        "config_sha256": config_sha256,
        "v1_constants": v1_constants,
        "directions": direction_rows(config, release_state, v1_constants),
        "release_state": release_state,
        "runtime_control": kill_switch,
        "redis": redis_summary,
        "findings": [item.as_dict() for item in findings],
    }
    report["success"] = not any(item.level == ERROR for item in findings)
    # Computed over everything above so the ledger digest covers the verdict as
    # well as the inputs; the digest can obviously not cover itself.
    report["report_sha256"] = sha256_of(report)
    return report


def record_report(engine, report: dict[str, Any]) -> None:
    findings_json = canonical_json(report["findings"])
    with engine.begin() as conn:
        conn.execute(text(LEDGER_DDL))
        for row in report["directions"]:
            conn.execute(text(LEDGER_UPSERT), {
                "release_tag": report["release_tag"],
                "stage": report["stage"],
                "direction": row["direction"],
                "execution_mode": row["execution_mode"],
                "legacy_max_candidates": row["legacy_max_candidates"],
                "legacy_top_n": row["legacy_top_n"],
                "legacy_max_candidates_updated_at": row["legacy_max_candidates_updated_at"],
                "legacy_top_n_updated_at": row["legacy_top_n_updated_at"],
                "legacy_config_source": row["legacy_config_source"],
                "v1_max_candidates": row["v1_max_candidates"],
                "v1_top_n": row["v1_top_n"],
                "sql_fetch_size": row["sql_fetch_size"],
                "config_sha256": report["config_sha256"],
                "report_sha256": report["report_sha256"],
                "success": 1 if report["success"] else 0,
                "findings": findings_json,
                "server_version": report["server_version"],
                "checked_by": report["checked_by"],
            })


def print_report(report: dict[str, Any]) -> None:
    print(f"recommendation-v1 preflight  tag={report['release_tag']}  stage={report['stage']}")
    print(f"  server_version : {report['server_version']}")
    print(f"  config_sha256  : {report['config_sha256']}")
    print(f"  report_sha256  : {report['report_sha256']}")
    print("  legacy config (read-only, never rewritten by this script):")
    for key, entry in sorted(report["legacy_config"].items()):
        print(
            f"    {key:24s} raw={entry['raw_value']!r} effective={entry['effective_value']} "
            f"source={entry['source']} updated_at={entry['updated_at']} updated_by={entry['updated_by']}"
        )
    print("  effective limits per direction:")
    for row in report["directions"]:
        print(
            f"    {row['direction']:14s} mode={row['execution_mode']:6s} "
            f"legacy={row['legacy_max_candidates']}/{row['legacy_top_n']} "
            f"v1={row['v1_max_candidates']}/{row['v1_top_n']} "
            f"sql_fetch_size={row['sql_fetch_size']}"
        )
    print("  findings:")
    for item in report["findings"]:
        print(f"    [{item['level']:5s}] {item['code']}: {item['message']}")
    print(f"  verdict: {'PASS' if report['success'] else 'FAIL'}")


def run(args) -> int:
    engine = create_engine(dsn_from_env(args.dsn_env), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            report = build_report(args, conn)

        if args.json:
            print(canonical_json(report))
        else:
            print_report(report)

        if args.record and not args.dry_run:
            record_report(engine, report)
            if not args.json:
                print(
                    f"  ledger: recorded {len(report['directions'])} row(s) into "
                    f"recommendation_preflight_ledger (success={1 if report['success'] else 0})"
                )
        elif args.record and args.dry_run:
            if not args.json:
                print("  ledger: --dry-run, nothing written")
    finally:
        engine.dispose()
    return 0 if report["success"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="recommendation_preflight.py",
        description=(
            "Recommendation-v1 launch preflight (§5.4.1 / §14.1): read and record the production "
            "match.max_candidates / match.top_n, validate them, and check the compatibility "
            "preconditions. Never writes system_config and never normalises the values to 50/3."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ordering per §11.1.1:\n"
            "  backup drill -> --stage pre-migration --record -> apply_phase9_migrations.py\n"
            "  -> --stage post-migration --record -> deploy with kill/off -> shadow\n"
        ),
    )
    parser.add_argument("--dsn-env", default="DB_URL", help="env var holding the SQLAlchemy DSN (default: DB_URL)")
    parser.add_argument("--check", action="store_true", help="report only, write nothing (read-only)")
    parser.add_argument("--record", action="store_true", help="report and upsert the release ledger row(s)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="with --record: print the report but skip the ledger write (--check is already read-only)",
    )
    parser.add_argument(
        "--release-tag", default=None,
        help="ledger key for this preflight run, e.g. rec-v1-2026-07-26 (required with --record)",
    )
    parser.add_argument(
        "--stage", choices=("pre-migration", "post-migration"), default="pre-migration",
        help="pre-migration tolerates absent v1 tables; post-migration requires them (default: pre-migration)",
    )
    parser.add_argument("--strict", action="store_true", help="promote every warning to a failure")
    parser.add_argument("--skip-redis", action="store_true", help="skip the §14.1 Redis compatibility samples")
    parser.add_argument(
        "--sample-size", type=int, default=200,
        help="how many Redis sessions / send-retry payloads to sample (default: 200)",
    )
    parser.add_argument("--json", action="store_true", help="emit the structured report instead of the text one")
    args = parser.parse_args()

    if args.check == args.record:
        parser.error("exactly one of --check/--record is required")
    if args.sample_size < 1:
        parser.error("--sample-size must be >= 1")
    if args.record and not args.release_tag:
        parser.error("--record requires --release-tag")
    if args.release_tag is None:
        args.release_tag = "adhoc-check"
    if len(args.release_tag) > 64:
        parser.error("--release-tag must be at most 64 characters")

    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
