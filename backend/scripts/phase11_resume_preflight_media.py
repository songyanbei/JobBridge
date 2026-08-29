"""Read-only canonical media preflight for the Phase 11 resume rollout.

The report contains fixed aggregate fields only. It deliberately omits entity
IDs, owners, storage references, canonical keys, URLs, and per-row errors.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select

# Direct execution puts backend/scripts, rather than backend, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, engine
from app.models import Job, MediaAssetLifecycle, Resume
from app.services.storage_reference_service import normalize_storage_reference


@dataclass(frozen=True)
class MediaTarget:
    entity_type: str
    entity_id: int
    owner_userid: str | None


@dataclass(frozen=True)
class LifecycleBinding:
    owner_userid: str | None
    entity_type: str | None
    entity_id: int | None


def _engine_loggers() -> set[logging.Logger]:
    loggers = {logging.getLogger("sqlalchemy.engine")}
    for name, candidate in logging.root.manager.loggerDict.items():
        if name.startswith("sqlalchemy.engine") and isinstance(candidate, logging.Logger):
            loggers.add(candidate)
    engine_logger = getattr(engine.logger, "logger", engine.logger)
    if isinstance(engine_logger, logging.Logger):
        loggers.add(engine_logger)
    return loggers


@contextmanager
def _mute_cli_database_logging() -> Iterator[None]:
    """Prevent SQL and bound values from escaping the aggregate-only CLI."""
    original_echo = engine.echo
    logger_states: dict[logging.Logger, tuple[bool, int, bool]] = {}

    def mute_known_loggers() -> None:
        for logger in _engine_loggers():
            logger_states.setdefault(
                logger,
                (logger.disabled, logger.level, logger.propagate),
            )
            logger.disabled = True
            logger.setLevel(logging.CRITICAL + 1)
            logger.propagate = False

    mute_known_loggers()
    engine.echo = False
    mute_known_loggers()
    try:
        yield
    finally:
        engine.echo = original_echo
        for logger, (disabled, level, propagate) in logger_states.items():
            logger.disabled = disabled
            logger.setLevel(level)
            logger.propagate = propagate


def _parse_images(images: Any) -> tuple[list[Any], bool]:
    if images is None:
        return [], False
    value = images
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return [], True
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [], True
    if not isinstance(value, list):
        return [], True
    return value, False


def _entity_pages(db, model, batch_size: int) -> Iterator[list[Any]]:
    last_id = 0
    while True:
        rows = db.execute(
            select(model.id, model.owner_userid, model.images)
            .where(model.id > last_id)
            .order_by(model.id)
            .limit(batch_size)
        ).all()
        if not rows:
            return
        yield rows
        last_id = int(rows[-1][0])


def _lifecycle_pages(db, batch_size: int) -> Iterator[list[Any]]:
    last_id = 0
    while True:
        rows = db.execute(
            select(
                MediaAssetLifecycle.id,
                MediaAssetLifecycle.object_key,
                MediaAssetLifecycle.owner_userid,
                MediaAssetLifecycle.entity_type,
                MediaAssetLifecycle.entity_id,
            )
            .where(MediaAssetLifecycle.id > last_id)
            .order_by(MediaAssetLifecycle.id)
            .limit(batch_size)
        ).all()
        if not rows:
            return
        yield rows
        last_id = int(rows[-1][0])


def _owner_matches(target: MediaTarget, binding: LifecycleBinding) -> bool:
    return (
        target.owner_userid is not None
        and binding.owner_userid is not None
        and target.owner_userid == binding.owner_userid
    )


def _entity_matches(target: MediaTarget, binding: LifecycleBinding) -> bool:
    return (
        binding.entity_type is not None
        and binding.entity_id is not None
        and target.entity_type == binding.entity_type
        and target.entity_id == binding.entity_id
    )


def collect_media_preflight(db, *, batch_size: int = 500) -> dict[str, int | bool]:
    """Collect canonical media integrity counts without mutating the database."""
    if batch_size <= 0:
        raise ValueError("batch_size_must_be_positive")

    target_occurrences: dict[str, dict[MediaTarget, int]] = {}
    resume_row_count = 0
    job_row_count = 0
    raw_reference_count = 0
    normalized_reference_count = 0
    invalid_images_payload_count = 0
    invalid_entity_media_reference_count = 0

    with db.no_autoflush:
        for entity_type, model in (("resume", Resume), ("job", Job)):
            for rows in _entity_pages(db, model, batch_size):
                if entity_type == "resume":
                    resume_row_count += len(rows)
                else:
                    job_row_count += len(rows)
                for entity_id, owner_userid, images in rows:
                    values, invalid_payload = _parse_images(images)
                    if invalid_payload:
                        invalid_images_payload_count += 1
                        continue
                    target = MediaTarget(
                        entity_type=entity_type,
                        entity_id=int(entity_id),
                        owner_userid=owner_userid,
                    )
                    raw_reference_count += len(values)
                    for raw_reference in values:
                        try:
                            canonical_key = normalize_storage_reference(raw_reference)
                        except (TypeError, ValueError):
                            invalid_entity_media_reference_count += 1
                            continue
                        normalized_reference_count += 1
                        targets = target_occurrences.setdefault(canonical_key, {})
                        targets[target] = targets.get(target, 0) + 1

        lifecycle_bindings: dict[str, list[LifecycleBinding]] = {}
        lifecycle_row_count = 0
        normalized_lifecycle_reference_count = 0
        invalid_lifecycle_reference_count = 0
        for rows in _lifecycle_pages(db, batch_size):
            lifecycle_row_count += len(rows)
            for _, object_key, owner_userid, entity_type, entity_id in rows:
                try:
                    canonical_key = normalize_storage_reference(object_key)
                except (TypeError, ValueError):
                    invalid_lifecycle_reference_count += 1
                    continue
                normalized_lifecycle_reference_count += 1
                lifecycle_bindings.setdefault(canonical_key, []).append(
                    LifecycleBinding(
                        owner_userid=owner_userid,
                        entity_type=entity_type,
                        entity_id=int(entity_id) if entity_id is not None else None,
                    )
                )

    same_resume_duplicate_groups = [
        occurrence_count
        for targets in target_occurrences.values()
        for target, occurrence_count in targets.items()
        if target.entity_type == "resume" and occurrence_count > 1
    ]
    shared_target_groups = [
        targets for targets in target_occurrences.values() if len(targets) > 1
    ]
    lifecycle_collision_groups = [
        bindings for bindings in lifecycle_bindings.values() if len(bindings) > 1
    ]

    missing_lifecycle_canonical_key_count = 0
    missing_lifecycle_target_key_count = 0
    owner_mismatch_target_key_count = 0
    entity_mismatch_target_key_count = 0
    binding_mismatch_target_key_count = 0
    fully_bound_target_key_count = 0

    for canonical_key, targets in target_occurrences.items():
        bindings = lifecycle_bindings.get(canonical_key)
        if not bindings:
            missing_lifecycle_canonical_key_count += 1
            missing_lifecycle_target_key_count += len(targets)
            continue
        for target in targets:
            owner_matches = any(_owner_matches(target, binding) for binding in bindings)
            entity_matches = any(_entity_matches(target, binding) for binding in bindings)
            fully_bound = any(
                _owner_matches(target, binding) and _entity_matches(target, binding)
                for binding in bindings
            )
            if not owner_matches:
                owner_mismatch_target_key_count += 1
            if not entity_matches:
                entity_mismatch_target_key_count += 1
            if fully_bound:
                fully_bound_target_key_count += 1
            else:
                binding_mismatch_target_key_count += 1

    blockers = (
        invalid_images_payload_count,
        invalid_entity_media_reference_count,
        sum(count - 1 for count in same_resume_duplicate_groups),
        sum(len(targets) for targets in shared_target_groups),
        invalid_lifecycle_reference_count,
        sum(len(bindings) for bindings in lifecycle_collision_groups),
        missing_lifecycle_target_key_count,
        binding_mismatch_target_key_count,
    )
    return {
        "resume_row_count": resume_row_count,
        "job_row_count": job_row_count,
        "entity_row_count": resume_row_count + job_row_count,
        "raw_entity_media_reference_count": raw_reference_count,
        "normalized_entity_media_reference_count": normalized_reference_count,
        "invalid_images_payload_count": invalid_images_payload_count,
        "invalid_entity_media_reference_count": invalid_entity_media_reference_count,
        "valid_entity_canonical_key_count": len(target_occurrences),
        "same_resume_canonical_duplicate_group_count": len(same_resume_duplicate_groups),
        "same_resume_canonical_duplicate_extra_reference_count": sum(
            count - 1 for count in same_resume_duplicate_groups
        ),
        "cross_entity_shared_canonical_key_count": len(shared_target_groups),
        "cross_entity_shared_target_key_count": sum(
            len(targets) for targets in shared_target_groups
        ),
        "lifecycle_row_count": lifecycle_row_count,
        "normalized_lifecycle_reference_count": normalized_lifecycle_reference_count,
        "invalid_lifecycle_reference_count": invalid_lifecycle_reference_count,
        "valid_lifecycle_canonical_key_count": len(lifecycle_bindings),
        "lifecycle_canonical_collision_key_count": len(lifecycle_collision_groups),
        "lifecycle_canonical_collision_row_count": sum(
            len(bindings) for bindings in lifecycle_collision_groups
        ),
        "missing_lifecycle_canonical_key_count": missing_lifecycle_canonical_key_count,
        "missing_lifecycle_target_key_count": missing_lifecycle_target_key_count,
        "owner_mismatch_target_key_count": owner_mismatch_target_key_count,
        "entity_mismatch_target_key_count": entity_mismatch_target_key_count,
        "binding_mismatch_target_key_count": binding_mismatch_target_key_count,
        "fully_bound_target_key_count": fully_bound_target_key_count,
        "ready": not any(blockers),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")

    try:
        with _mute_cli_database_logging():
            db = SessionLocal()
            try:
                report = collect_media_preflight(db, batch_size=args.batch_size)
            finally:
                try:
                    db.rollback()
                finally:
                    db.close()
    except Exception:
        print(
            json.dumps(
                {"error": "resume_media_preflight_failed", "ready": False},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
