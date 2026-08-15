"""Check MySQL and Redis clock skew before the Phase 10 rollout."""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text

from app.core.redis_client import get_redis
from app.db import SessionLocal


MAX_CLOCK_SKEW_SECONDS = Decimal("2")


def _redis_epoch(client) -> Decimal:
    seconds, microseconds = client.time()
    return Decimal(str(seconds)) + Decimal(str(microseconds)) / Decimal("1000000")


def collect_clock_report(db, redis_client=None) -> dict[str, float | bool]:
    client = redis_client or get_redis()
    redis_before = _redis_epoch(client)
    mysql_epoch = Decimal(str(db.execute(
        text("SELECT UNIX_TIMESTAMP(NOW(6))")
    ).scalar_one()))
    redis_after = _redis_epoch(client)
    sampling_window = abs(redis_after - redis_before)
    skew = max(
        abs(mysql_epoch - redis_before),
        abs(mysql_epoch - redis_after),
    )
    return {
        "mysql_epoch": float(mysql_epoch),
        "redis_epoch_before": float(redis_before),
        "redis_epoch_after": float(redis_after),
        "sampling_window_seconds": float(sampling_window),
        "clock_skew_seconds": float(skew),
        "max_clock_skew_seconds": float(MAX_CLOCK_SKEW_SECONDS),
        "ready": skew <= MAX_CLOCK_SKEW_SECONDS,
    }


def main() -> int:
    with SessionLocal() as db:
        report = collect_clock_report(db)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
