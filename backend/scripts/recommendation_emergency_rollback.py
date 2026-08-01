"""Emergency "roll back and invalidate every strategy snapshot" command (§7.4).

§7.4 keeps normal rollback in the admin UI but deliberately leaves this one out
of it: routine rollback must *not* invalidate user snapshots, because that makes
pagination jump under people's fingers.  Only a compliance or severe-quality
incident justifies dropping snapshots mid-conversation, and that is what this
command is for.

What it does, in one database transaction plus one Redis sweep:

1. engage the global kill switch (default on -- it is the fastest stop and it
   covers both directions at once; ``--no-kill-switch`` opts out);
2. for each ``--rollback DIRECTION=REVISION``, read the immutable
   ``recommendation_release_history`` snapshot for that target revision and copy
   it forward as ``current_revision + 1`` with ``operation=rollback`` and
   ``target_revision`` recorded -- never by rewinding ``revision``;
3. write ``audit_log`` rows (``strategy_rollback`` / ``strategy_kill_switch``);
4. sweep Redis and drop the v1 candidate snapshots so ``show_more`` stops paging
   through a v1 ordering that is no longer allowed to serve.

Versions, deliveries and exposure facts are never deleted (§7.4).

Nothing happens without ``--yes``: the default is a dry run that prints the exact
release rows, history rows, audit rows and snapshot counts it would produce.

Standard commands::

    cd backend
    python scripts/recommendation_emergency_rollback.py --dsn-env DB_URL \
        --rollback search_job=7 --operator alice --reason "compliance incident 2026-07-26"
    python scripts/recommendation_emergency_rollback.py --dsn-env DB_URL \
        --rollback search_job=7 --rollback search_worker=4 \
        --operator alice --reason "compliance incident 2026-07-26" --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# `python scripts/recommendation_emergency_rollback.py` puts `backend/scripts` on
# sys.path, not `backend`, so the `app` package has to be made importable before
# the imports below.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.time_utils import to_naive_utc, utc_now  # noqa: E402
from app.models import (  # noqa: E402
    RecommendationReleaseHistory,
    RecommendationRuntimeControl,
    RecommendationStrategyRelease,
    RecommendationStrategyVersion,
)
from app.services.admin_log_service import write_admin_log  # noqa: E402


DIRECTIONS = ("search_job", "search_worker")

SNAPSHOT_SCOPES = ("rolled-back", "all-v1", "all")

# A worker that was already mid-turn when the sweep ran can still hold an
# in-memory v1 snapshot and try to commit it afterwards.  Its CAS is fenced by
# the bumped ``session_version`` (see ``_SWEEP_CAS_SCRIPT``), but a turn that
# started *after* the bump and *before* the kill switch reached that process
# could still write a fresh one, so the sweep runs more than once by default.
DEFAULT_SWEEP_PASSES = 2

# Raw compare-and-swap on the exact previous value: if anything changed the
# session between our GET and our SET we skip it and let the next pass retry.
# PTTL/PSETEX rather than SET..KEEPTTL so the script does not require Redis 6.
_SWEEP_CAS_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
    return 0
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl and ttl > 0 then
    redis.call('PSETEX', KEYS[1], ttl, ARGV[2])
else
    redis.call('SET', KEYS[1], ARGV[2])
end
return 1
"""


@dataclass(frozen=True)
class RollbackTarget:
    direction: str
    target_revision: int


@dataclass
class PlannedRollback:
    direction: str
    target_revision: int
    before: dict[str, Any]
    after: dict[str, Any]


def dsn_from_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing DSN environment variable: {name}")
    return value


def parse_rollback(raw: str) -> RollbackTarget:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"expected DIRECTION=REVISION, got {raw!r}")
    direction, _, revision = raw.partition("=")
    direction = direction.strip()
    if direction not in DIRECTIONS:
        raise argparse.ArgumentTypeError(f"unknown direction {direction!r}; expected one of {list(DIRECTIONS)}")
    try:
        parsed = int(revision.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"target revision must be an integer, got {revision!r}") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"target revision must be positive, got {parsed}")
    return RollbackTarget(direction=direction, target_revision=parsed)


def release_snapshot(row: RecommendationStrategyRelease) -> dict[str, Any]:
    return {
        "direction": row.direction,
        "execution_mode": row.execution_mode,
        "stable_version_id": row.stable_version_id,
        "candidate_version_id": row.candidate_version_id,
        "rollout_percentage": row.rollout_percentage,
        "revision": row.revision,
        "lock_version": row.lock_version,
    }


# ---------------------------------------------------------------------------
# database side
# ---------------------------------------------------------------------------

def plan_rollbacks(db: Session, targets: list[RollbackTarget]) -> list[PlannedRollback]:
    """Resolve every target against the immutable history without writing.

    Runs in full before any mutation so an unusable target aborts the whole
    command instead of leaving one direction rolled back and the other not.
    """
    planned: list[PlannedRollback] = []
    for target in targets:
        release = (
            db.query(RecommendationStrategyRelease)
            .filter(RecommendationStrategyRelease.direction == target.direction)
            .with_for_update()
            .first()
        )
        if release is None:
            raise SystemExit(
                f"no recommendation_strategy_release row for {target.direction}; "
                "there is nothing to roll back (the empty-table fallback already serves legacy)"
            )

        history = (
            db.query(RecommendationReleaseHistory)
            .filter(
                RecommendationReleaseHistory.direction == target.direction,
                RecommendationReleaseHistory.revision == target.target_revision,
            )
            .first()
        )
        if history is None:
            available = [
                int(row[0]) for row in db.query(RecommendationReleaseHistory.revision)
                .filter(RecommendationReleaseHistory.direction == target.direction)
                .order_by(RecommendationReleaseHistory.revision.desc())
                .limit(20)
                .all()
            ]
            raise SystemExit(
                f"{target.direction}: no recommendation_release_history row for revision "
                f"{target.target_revision}; recent revisions are {available}"
            )
        if int(target.target_revision) > int(release.revision):
            raise SystemExit(
                f"{target.direction}: target revision {target.target_revision} is ahead of the current "
                f"revision {release.revision}; §7.4 rollback only replays an already-recorded snapshot"
            )

        # §7.4: `stable_version_id=NULL` in the target snapshot is a legal legacy
        # state, so only non-null ids are required to still exist.
        for column in ("stable_version_id", "candidate_version_id"):
            version_id = getattr(history, column)
            if version_id is None:
                continue
            if db.get(RecommendationStrategyVersion, int(version_id)) is None:
                raise SystemExit(
                    f"{target.direction}: history revision {target.target_revision} points at "
                    f"{column}={version_id}, which no longer exists in recommendation_strategy_version"
                )

        before = release_snapshot(release)
        after = {
            "direction": target.direction,
            "execution_mode": history.execution_mode,
            "stable_version_id": history.stable_version_id,
            "candidate_version_id": history.candidate_version_id,
            "rollout_percentage": history.rollout_percentage,
            "revision": int(release.revision) + 1,
            "lock_version": int(release.lock_version) + 1,
        }
        planned.append(PlannedRollback(
            direction=target.direction,
            target_revision=target.target_revision,
            before=before,
            after=after,
        ))
    return planned


def apply_rollbacks(db: Session, planned: list[PlannedRollback], operator: str, reason: str) -> None:
    for item in planned:
        release = (
            db.query(RecommendationStrategyRelease)
            .filter(
                RecommendationStrategyRelease.direction == item.direction,
                RecommendationStrategyRelease.lock_version == item.before["lock_version"],
            )
            .with_for_update()
            .first()
        )
        if release is None:
            raise SystemExit(
                f"{item.direction}: lock_version changed since the plan was built "
                f"(expected {item.before['lock_version']}); rerun the command"
            )

        release.execution_mode = item.after["execution_mode"]
        release.stable_version_id = item.after["stable_version_id"]
        release.candidate_version_id = item.after["candidate_version_id"]
        release.rollout_percentage = item.after["rollout_percentage"]
        release.revision = item.after["revision"]
        release.lock_version = item.after["lock_version"]
        release.updated_by = operator
        release.updated_at = to_naive_utc(utc_now())

        db.add(RecommendationReleaseHistory(
            direction=item.direction,
            revision=item.after["revision"],
            operation="rollback",
            execution_mode=item.after["execution_mode"],
            stable_version_id=item.after["stable_version_id"],
            candidate_version_id=item.after["candidate_version_id"],
            rollout_percentage=item.after["rollout_percentage"],
            target_revision=item.target_revision,
            change_reason=reason[:255],
            created_by=operator,
        ))

        write_admin_log(
            db,
            target_type="recommendation_strategy",
            target_id=item.direction,
            action="strategy_rollback",
            operator=operator,
            before=item.before,
            after=dict(item.after, target_revision=item.target_revision),
            reason=f"emergency_rollback: {reason}"[:255],
        )


def plan_kill_switch(db: Session) -> dict[str, Any] | None:
    """Return the pending kill-switch change, or None when it is already on."""
    control = (
        db.query(RecommendationRuntimeControl)
        .filter(RecommendationRuntimeControl.scope == "global")
        .with_for_update()
        .first()
    )
    if control is None:
        raise SystemExit(
            "no recommendation_runtime_control row for scope=global; "
            "run recommendation_strategy_service.ensure_initial_release before using this command"
        )
    if bool(control.kill_switch):
        return None
    return {
        "before": {
            "kill_switch": False,
            "revision": int(control.revision),
            "lock_version": int(control.lock_version),
        },
        "after": {
            "kill_switch": True,
            "revision": int(control.revision) + 1,
            "lock_version": int(control.lock_version) + 1,
        },
    }


def apply_kill_switch(db: Session, plan: dict[str, Any], operator: str, reason: str) -> None:
    control = (
        db.query(RecommendationRuntimeControl)
        .filter(
            RecommendationRuntimeControl.scope == "global",
            RecommendationRuntimeControl.lock_version == plan["before"]["lock_version"],
        )
        .with_for_update()
        .first()
    )
    if control is None:
        raise SystemExit(
            "global runtime control lock_version changed since the plan was built; rerun the command"
        )
    control.kill_switch = True
    control.revision = plan["after"]["revision"]
    control.lock_version = plan["after"]["lock_version"]
    control.change_reason = f"emergency_rollback: {reason}"[:255]
    control.updated_by = operator
    control.updated_at = to_naive_utc(utc_now())

    write_admin_log(
        db,
        target_type="recommendation_strategy",
        target_id="global",
        action="strategy_kill_switch",
        operator=operator,
        before=plan["before"],
        after=plan["after"],
        reason=f"emergency_rollback: {reason}"[:255],
    )


# ---------------------------------------------------------------------------
# Redis snapshot sweep
# ---------------------------------------------------------------------------

def _snapshot_is_v1(snapshot: dict[str, Any]) -> bool:
    return (
        str(snapshot.get("algorithm_version") or "legacy") != "legacy"
        or str(snapshot.get("assignment") or "legacy") != "legacy"
    )


def _should_clear(snapshot: dict[str, Any], scope: str, directions: set[str]) -> bool:
    if scope == "all":
        return True
    if not _snapshot_is_v1(snapshot):
        return False
    if scope == "all-v1":
        return True
    # scope == "rolled-back": a v1 snapshot whose direction was not recorded
    # cannot be proven to belong to a direction we are keeping, so it is cleared
    # too -- leaving it would let show_more keep paging a v1 ordering.
    direction = snapshot.get("direction")
    return not direction or str(direction) in directions


def sweep_snapshots(
    scope: str,
    directions: set[str],
    passes: int,
    dry_run: bool,
    scan_count: int,
) -> dict[str, Any]:
    """Clear matching candidate snapshots from every live Redis session.

    The whole session is preserved -- only ``candidate_snapshot`` / ``shown_items``
    are reset -- so conversation history and accumulated criteria survive.
    ``session_version`` is bumped on purpose: that is what makes an in-flight
    worker's ``save_session_if_version`` CAS fail instead of resurrecting the v1
    snapshot we just dropped.
    """
    from app.core.redis_client import SESSION_PREFIX, get_redis

    client = get_redis()
    totals = {"scanned": 0, "matched": 0, "cleared": 0, "cas_conflicts": 0, "unreadable": 0}
    per_pass: list[dict[str, int]] = []

    for _ in range(max(passes, 1)):
        stats = {"scanned": 0, "matched": 0, "cleared": 0, "cas_conflicts": 0, "unreadable": 0}
        for key in client.scan_iter(match=f"{SESSION_PREFIX}*", count=scan_count):
            name = key if isinstance(key, str) else key.decode("utf-8")
            stats["scanned"] += 1
            raw = client.get(name)
            if raw is None:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                stats["unreadable"] += 1
                continue
            snapshot = payload.get("candidate_snapshot")
            if not isinstance(snapshot, dict):
                continue
            if not _should_clear(snapshot, scope, directions):
                continue
            stats["matched"] += 1
            if dry_run:
                continue

            payload["candidate_snapshot"] = None
            payload["shown_items"] = []
            # Mirrors `conversation_service._self_heal_active_flow`: search_active
            # without a snapshot is not a valid state to persist.
            if payload.get("active_flow") == "search_active":
                payload["active_flow"] = "idle"
            payload["session_version"] = int(payload.get("session_version") or 0) + 1
            payload["updated_at"] = utc_now().isoformat()

            updated = json.dumps(payload, ensure_ascii=False)
            if int(client.eval(_SWEEP_CAS_SCRIPT, 1, name, raw, updated)) == 1:
                stats["cleared"] += 1
            else:
                stats["cas_conflicts"] += 1

        per_pass.append(stats)
        for field_name in totals:
            totals[field_name] += stats[field_name]
        if dry_run:
            break

    return {"total": totals, "passes": per_pass}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def describe_plan(planned: list[PlannedRollback], kill_plan: dict[str, Any] | None, kill_requested: bool) -> None:
    if kill_requested:
        if kill_plan is None:
            print("  kill switch : already engaged, nothing to change")
        else:
            print(
                f"  kill switch : off -> on (revision {kill_plan['before']['revision']} -> "
                f"{kill_plan['after']['revision']}) + audit_log strategy_kill_switch"
            )
    else:
        print("  kill switch : --no-kill-switch, left untouched")

    for item in planned:
        before, after = item.before, item.after
        print(f"  {item.direction}:")
        print(
            f"    release  : mode {before['execution_mode']} -> {after['execution_mode']}, "
            f"stable {before['stable_version_id']} -> {after['stable_version_id']}, "
            f"candidate {before['candidate_version_id']} -> {after['candidate_version_id']}, "
            f"rollout {before['rollout_percentage']}% -> {after['rollout_percentage']}%"
        )
        print(
            f"    revision : {before['revision']} -> {after['revision']} "
            f"(new revision, target_revision={item.target_revision}; the current revision is never rewound)"
        )
        print(f"    history  : + recommendation_release_history revision={after['revision']} operation=rollback")
        print(f"    audit    : + audit_log strategy_rollback target_id={item.direction}")


def run(args) -> int:
    targets = args.rollback or []
    seen = [target.direction for target in targets]
    duplicates = sorted({name for name in seen if seen.count(name) > 1})
    if duplicates:
        raise SystemExit(f"each direction may only be rolled back once per run, got duplicates: {duplicates}")

    directions = {target.direction for target in targets}
    dry_run = not args.yes

    print("recommendation-v1 EMERGENCY rollback (§7.4)")
    print(f"  mode      : {'DRY RUN (no changes; pass --yes to execute)' if dry_run else 'EXECUTE'}")
    print(f"  operator  : {args.operator}")
    print(f"  reason    : {args.reason}")

    engine = create_engine(dsn_from_env(args.dsn_env), pool_pre_ping=True)
    try:
        with Session(bind=engine) as db:
            planned = plan_rollbacks(db, targets)
            kill_plan = plan_kill_switch(db) if args.kill_switch else None
            describe_plan(planned, kill_plan, args.kill_switch)

            if dry_run:
                db.rollback()
            else:
                if args.kill_switch and kill_plan is not None:
                    apply_kill_switch(db, kill_plan, args.operator, args.reason)
                apply_rollbacks(db, planned, args.operator, args.reason)
                db.commit()
                print("  database  : committed")
                if args.kill_switch:
                    print(
                        "  WARNING   : this script only writes the database source of truth. If the "
                        "runtime distributes the kill switch through a Redis cache, invalidate it "
                        "(or wait for its TTL) before declaring the incident contained."
                    )
    finally:
        engine.dispose()

    if args.no_invalidate_snapshots:
        print("  snapshots : --no-invalidate-snapshots, Redis left untouched")
        return 0

    print(f"  snapshots : scope={args.snapshot_scope} passes={args.sweep_passes}")
    try:
        result = sweep_snapshots(
            scope=args.snapshot_scope,
            directions=directions,
            passes=args.sweep_passes,
            dry_run=dry_run,
            scan_count=args.scan_count,
        )
    except Exception as exc:
        # The database rollback already landed, so the bleeding is stopped; the
        # sweep failing is loud but must not be reported as "nothing happened".
        print(
            f"  snapshots : FAILED ({exc.__class__.__name__}: {exc}). The database rollback "
            "is committed; rerun with --no-kill-switch and the same --rollback arguments once "
            "Redis is reachable to finish invalidating snapshots.",
            file=sys.stderr,
        )
        return 2

    total = result["total"]
    print(
        f"    scanned={total['scanned']} matched={total['matched']} cleared={total['cleared']} "
        f"cas_conflicts={total['cas_conflicts']} unreadable={total['unreadable']}"
    )
    if dry_run:
        print("    (dry run: one pass, nothing written)")
    elif total["cas_conflicts"]:
        print(
            f"    {total['cas_conflicts']} session(s) changed mid-sweep and were skipped; "
            "rerun the command to sweep them once traffic has settled."
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="recommendation_emergency_rollback.py",
        description=(
            "Emergency recommendation-v1 rollback that also invalidates live strategy snapshots "
            "(§7.4). Deliberately not exposed as an admin button: it makes show_more pagination "
            "restart for every affected user. Dry run unless --yes is given."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # dry run, both directions\n"
            "  python scripts/recommendation_emergency_rollback.py --dsn-env DB_URL \\\n"
            "      --rollback search_job=7 --rollback search_worker=4 \\\n"
            "      --operator alice --reason 'compliance incident'\n"
            "  # execute\n"
            "  ... --yes\n"
        ),
    )
    parser.add_argument("--dsn-env", default="DB_URL", help="env var holding the SQLAlchemy DSN (default: DB_URL)")
    parser.add_argument(
        "--rollback", action="append", type=parse_rollback, metavar="DIRECTION=REVISION",
        help=(
            "explicit target, e.g. search_job=7. Repeatable (once per direction). §7.4 forbids an "
            "implicit 'previous stable' pointer, so the revision is always spelled out."
        ),
    )
    parser.add_argument("--operator", required=True, help="admin username recorded in audit_log / release history")
    parser.add_argument("--reason", required=True, help="incident reason recorded in audit_log / release history")
    parser.add_argument(
        "--kill-switch", action=argparse.BooleanOptionalAction, default=True,
        help="also engage the global kill switch (default: yes; it stops both directions immediately)",
    )
    parser.add_argument(
        "--no-invalidate-snapshots", action="store_true",
        help="skip the Redis snapshot sweep (database rollback only)",
    )
    parser.add_argument(
        "--snapshot-scope", choices=SNAPSHOT_SCOPES, default="rolled-back",
        help=(
            "rolled-back: v1 snapshots of the rolled-back directions plus v1 snapshots with no "
            "recorded direction; all-v1: every non-legacy snapshot; all: every snapshot "
            "(default: rolled-back)"
        ),
    )
    parser.add_argument(
        "--sweep-passes", type=int, default=DEFAULT_SWEEP_PASSES,
        help=f"Redis sweep passes, to catch in-flight workers (default: {DEFAULT_SWEEP_PASSES})",
    )
    parser.add_argument("--scan-count", type=int, default=200, help="Redis SCAN COUNT hint (default: 200)")
    parser.add_argument(
        "--yes", action="store_true",
        help="actually execute; without it the command only prints the plan",
    )
    args = parser.parse_args()

    if not args.rollback and not args.kill_switch:
        parser.error("nothing to do: pass at least one --rollback, or keep the default --kill-switch")
    if args.sweep_passes < 1:
        parser.error("--sweep-passes must be >= 1")
    if args.scan_count < 1:
        parser.error("--scan-count must be >= 1")
    if not args.operator.strip():
        parser.error("--operator must not be empty")
    if not args.reason.strip():
        parser.error("--reason must not be empty")

    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
