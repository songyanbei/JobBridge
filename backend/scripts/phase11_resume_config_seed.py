"""Atomically validate and seed the three Phase 11 database settings."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import bindparam, create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.phase11_cli_safety import run_safely  # noqa: E402
from app.services.lifecycle_config_service import parse_canonical_ascii_uint  # noqa: E402
from app.services.resume_replacement_rollout_service import (  # noqa: E402
    LEGACY_ROLLOUT_CONFIG_KEYS,
    ROLLOUT_CONFIG_KEY,
    validate_allowlist,
)

MIGRATION_KEY = "phase11_resume_config_seed"
DEFAULTS = {
    "ttl.resume.days": ("30", "int", "简历业务有效期（天）"),
    "ttl.resume.candidate.days": ("7", "int", "简历候选版本保留期（天）"),
    ROLLOUT_CONFIG_KEY: (
        '{"revision":1,"userids":[]}', "json", "简历替换隐藏 allowlist",
    ),
}


def _validate(key: str, value: str, value_type: str) -> None:
    if key == "ttl.resume.days":
        try:
            if value_type != "int":
                raise ValueError("wrong type")
            parse_canonical_ascii_uint(value, lower=1, upper=3650)
        except ValueError:
            raise RuntimeError("invalid_existing_resume_ttl")
    elif key == "ttl.resume.candidate.days":
        try:
            if value_type != "int":
                raise ValueError("wrong type")
            parse_canonical_ascii_uint(value, lower=1, upper=365)
        except ValueError:
            raise RuntimeError("invalid_existing_candidate_ttl")
    elif key in LEGACY_ROLLOUT_CONFIG_KEYS:
        raise RuntimeError("legacy_rollout_config_present")
    elif key == ROLLOUT_CONFIG_KEY:
        if value_type != "json":
            raise RuntimeError("invalid_existing_rollout_allowlist")
        try:
            validate_allowlist(json.loads(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid_existing_rollout_allowlist") from exc
    else:  # The SELECT below is intentionally closed over a fixed key set.
        raise RuntimeError("unknown_phase11_resume_config")


def seed(dsn: str, *, apply: bool) -> dict:
    engine = create_engine(dsn, pool_pre_ping=True, hide_parameters=True)
    with engine.begin() as conn:
        validation_keys = (
            "ttl.resume.days",
            "ttl.resume.candidate.days",
            ROLLOUT_CONFIG_KEY,
            *LEGACY_ROLLOUT_CONFIG_KEYS,
        )
        rows = conn.execute(text("""SELECT config_key,config_value,value_type FROM system_config
          WHERE config_key IN :keys FOR UPDATE""").bindparams(
              bindparam("keys", expanding=True)
          ), {"keys": validation_keys}).mappings().all()
        for row in rows:
            _validate(str(row["config_key"]), str(row["config_value"]), str(row["value_type"]))
        if apply:
            for key, (value, value_type, description) in DEFAULTS.items():
                conn.execute(text("""INSERT IGNORE INTO system_config
                  (config_key,config_value,value_type,description)
                  VALUES(:key,:value,:type,:description)"""), {
                    "key": key, "value": value, "type": value_type, "description": description,
                })
    return {"status": "succeeded", "config_cursor": {"seeded": bool(apply)},
            "existing": len(rows), "dry_run": not apply}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True); parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume-cursor-json", default="{}")
    args = parser.parse_args()
    print(json.dumps(seed(args.dsn, apply=args.apply), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_safely(main, "phase11_config_seed_failed"))
