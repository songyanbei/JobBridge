"""Audit and optionally backfill historical Job/Resume media lifecycle rows.

The command is dry-run by default.  It always writes a per-reference CSV and a
JSON summary; ``--apply`` performs only idempotent, unambiguous repairs.
"""
from __future__ import annotations

import argparse
import csv
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.config import settings
from app.db import SessionLocal
from app.models import Job, MediaAssetLifecycle, Resume
from app.services.storage_reference_service import normalize_storage_reference


DETAIL_FIELDS = (
    "entity_type",
    "entity_id",
    "array_index",
    "raw_value",
    "classification",
    "normalized_object_key",
    "storage_origin",
    "result",
    "error_code",
    "migration_batch_id",
)


def _parse_images(images: Any) -> tuple[list[Any], str | None]:
    if images is None:
        return [], None
    value = images
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return [], "invalid_images_encoding"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [], "invalid_images_json"
    if not isinstance(value, list):
        return [], "images_not_array"
    return value, None


def _classify(raw_value: Any) -> dict[str, str | None]:
    classification = "invalid"
    storage_origin = None
    try:
        if not isinstance(raw_value, str):
            raise ValueError("invalid_media_reference")
        parsed = urlsplit(raw_value)
        if parsed.scheme or parsed.netloc:
            classification = "absolute_url"
            storage_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        elif raw_value.startswith("/"):
            classification = "local_access_url"
            storage_origin = settings.oss_provider
        else:
            classification = "object_key"
            storage_origin = settings.oss_provider
        key = normalize_storage_reference(raw_value)
        if classification == "absolute_url":
            classification = "trusted_absolute_url"
        return {
            "classification": classification,
            "normalized_object_key": key,
            "storage_origin": storage_origin,
            "error_code": None,
        }
    except (TypeError, ValueError) as exc:
        error_code = str(exc) or type(exc).__name__
        if error_code == "external_url":
            classification = "external_url"
        return {
            "classification": classification,
            "normalized_object_key": None,
            "storage_origin": storage_origin,
            "error_code": error_code,
        }


def _collect_key_targets(db, batch_size: int) -> dict[str, set[tuple[str, int]]]:
    """Build the complete ownership graph before apply mutates any row."""
    targets: dict[str, set[tuple[str, int]]] = {}
    for entity_type, model in (("job", Job), ("resume", Resume)):
        last_id = 0
        while True:
            rows = (
                db.query(model)
                .filter(model.id > last_id)
                .order_by(model.id)
                .limit(batch_size)
                .all()
            )
            if not rows:
                break
            for entity in rows:
                values, parse_error = _parse_images(entity.images)
                if parse_error:
                    continue
                for raw_value in values:
                    key = _classify(raw_value)["normalized_object_key"]
                    if key is not None:
                        targets.setdefault(key, set()).add((entity_type, int(entity.id)))
            last_id = int(rows[-1].id)
            if len(rows) < batch_size:
                break
    return targets


def _new_detail(
    *,
    entity_type: str,
    entity_id: int,
    array_index: int,
    raw_value: Any,
    migration_batch_id: str,
    analysis: dict[str, str | None],
    result: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "array_index": array_index,
        "raw_value": raw_value,
        "classification": analysis["classification"],
        "normalized_object_key": analysis["normalized_object_key"],
        "storage_origin": analysis["storage_origin"],
        "result": result,
        "error_code": error_code if error_code is not None else analysis["error_code"],
        "migration_batch_id": migration_batch_id,
    }


def _desired_state(entity) -> str:
    return "delete_pending" if entity.deleted_at is not None else "attached"


def _repair_existing(
    row: MediaAssetLifecycle,
    *,
    entity_type: str,
    entity,
    desired_state: str,
    apply: bool,
    now: datetime,
) -> tuple[str, str | None, bool]:
    target = (row.entity_type, int(row.entity_id) if row.entity_id is not None else None)
    expected = (entity_type, int(entity.id))
    unbound = target == (None, None)
    if row.owner_userid != entity.owner_userid:
        return "conflict", "media_owner_mismatch", False
    if target != expected and not unbound:
        return "conflict", "media_key_bound_to_other_entity", False

    if desired_state == "attached" and row.state in {
        "delete_pending",
        "deleted",
        "dead_letter",
    }:
        return "conflict", f"active_entity_media_state_{row.state}", False

    if row.state == "dead_letter":
        return "matched", "media_delete_dead_letter_requires_manual_recovery", False

    if desired_state == "delete_pending" and row.state == "deleted":
        if not unbound:
            return "matched", None, False
        if apply:
            row.entity_type = entity_type
            row.entity_id = entity.id
            row.draft_expires_at = None
            return "updated", None, True
        return "would_update", None, True

    needs_update = unbound or row.state != desired_state
    if not needs_update:
        return "matched", None, False
    if apply:
        row.entity_type = entity_type
        row.entity_id = entity.id
        row.draft_expires_at = None
        row.state = desired_state
        row.next_attempt_at = now if desired_state == "delete_pending" else None
        return "updated", None, True
    return "would_update", None, True


def _repair_unreferenced_soft_deleted_media(
    db,
    *,
    key_targets: dict[str, set[tuple[str, int]]],
    soft_deleted_targets: set[tuple[str, int]],
    apply: bool,
    batch_size: int,
    migration_batch_id: str,
    now: datetime,
    summary: dict[str, Any],
    details: list[dict[str, Any]],
    blocked_existing_keys: dict[str, str],
) -> None:
    """Reconcile bound lifecycle rows no longer present in entity.images."""
    last_id = 0
    while True:
        rows = (
            db.query(MediaAssetLifecycle)
            .filter(MediaAssetLifecycle.id > last_id)
            .order_by(MediaAssetLifecycle.id)
            .limit(batch_size)
            .all()
        )
        if not rows:
            break
        for row in rows:
            if row.entity_type not in {"job", "resume"} or row.entity_id is None:
                continue
            target = (row.entity_type, int(row.entity_id))
            if target not in soft_deleted_targets:
                continue
            references = key_targets.get(row.object_key, set())
            if target in references:
                continue

            analysis = {
                "classification": "object_key",
                "normalized_object_key": row.object_key,
                "storage_origin": settings.oss_provider,
                "error_code": None,
            }
            if references:
                error = "soft_deleted_media_key_referenced_by_other_entity"
                if row.object_key not in blocked_existing_keys:
                    summary["media_reference_conflict_count"] += 1
                    blocked_existing_keys[row.object_key] = error
                details.append(_new_detail(
                    entity_type=row.entity_type,
                    entity_id=int(row.entity_id),
                    array_index=-1,
                    raw_value=row.object_key,
                    migration_batch_id=migration_batch_id,
                    analysis=analysis,
                    result="conflict",
                    error_code=error,
                ))
                continue

            if row.state == "deleted":
                continue
            summary["non_deleted_soft_deleted_media_key_count"] += 1
            needs_update = row.state in {"pending", "attached"}
            result = "matched"
            if needs_update:
                summary["repair_required_before_backfill_key_count"] += 1
                if apply:
                    row.state = "delete_pending"
                    row.next_attempt_at = now
                    summary["updated_media_lifecycle_count"] += 1
                    result = "updated"
                else:
                    summary["repair_required_media_lifecycle_key_count"] += 1
                    result = "would_update"
            details.append(_new_detail(
                entity_type=row.entity_type,
                entity_id=int(row.entity_id),
                array_index=-1,
                raw_value=row.object_key,
                migration_batch_id=migration_batch_id,
                analysis=analysis,
                result=result,
            ))

        if apply:
            db.commit()
        last_id = int(rows[-1].id)
        if len(rows) < batch_size:
            break


def backfill_media_lifecycle(
    db,
    *,
    apply: bool = False,
    batch_size: int = 500,
    migration_batch_id: str | None = None,
) -> dict[str, Any]:
    """Analyze all media references and optionally repair lifecycle coverage."""
    if batch_size < 1:
        raise ValueError("batch_size_must_be_positive")
    batch_id = migration_batch_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    details: list[dict[str, Any]] = []
    key_targets = _collect_key_targets(db, batch_size)
    ambiguous_keys = {key for key, targets in key_targets.items() if len(targets) > 1}
    reported_ambiguous_keys: set[str] = set()
    blocked_existing_keys: dict[str, str] = {}
    seen_targets: dict[str, tuple[str, int]] = {}
    existing_cache: dict[str, MediaAssetLifecycle | None] = {}
    unique_by_type: dict[str, set[str]] = {"job": set(), "resume": set()}
    soft_deleted_targets: set[tuple[str, int]] = set()
    summary: dict[str, Any] = {
        "migration_batch_id": batch_id,
        "apply": apply,
        "entity_rows_scanned": 0,
        "raw_reference_count": 0,
        "normalized_reference_count": 0,
        "normalized_job_image_key_count": 0,
        "normalized_resume_image_key_count": 0,
        "matched_media_lifecycle_key_count": 0,
        "missing_before_backfill_key_count": 0,
        "missing_media_lifecycle_key_count": 0,
        "repair_required_before_backfill_key_count": 0,
        "repair_required_media_lifecycle_key_count": 0,
        "non_deleted_soft_deleted_media_key_count": 0,
        "invalid_images_json_count": 0,
        "unresolved_media_reference_count": 0,
        "media_reference_alias_count": 0,
        "media_reference_conflict_count": 0,
        "created_media_lifecycle_count": 0,
        "updated_media_lifecycle_count": 0,
    }

    for entity_type, model in (("job", Job), ("resume", Resume)):
        last_id = 0
        while True:
            rows = (
                db.query(model)
                .filter(model.id > last_id)
                .order_by(model.id)
                .limit(batch_size)
                .all()
            )
            if not rows:
                break
            for entity in rows:
                summary["entity_rows_scanned"] += 1
                if entity.deleted_at is not None:
                    soft_deleted_targets.add((entity_type, int(entity.id)))
                values, parse_error = _parse_images(entity.images)
                if parse_error:
                    summary["invalid_images_json_count"] += 1
                    details.append(_new_detail(
                        entity_type=entity_type,
                        entity_id=int(entity.id),
                        array_index=-1,
                        raw_value=entity.images,
                        migration_batch_id=batch_id,
                        analysis={
                            "classification": "invalid_images",
                            "normalized_object_key": None,
                            "storage_origin": None,
                            "error_code": parse_error,
                        },
                        result="unresolved",
                    ))
                    continue

                for index, raw_value in enumerate(values):
                    summary["raw_reference_count"] += 1
                    analysis = _classify(raw_value)
                    key = analysis["normalized_object_key"]
                    if key is None:
                        summary["unresolved_media_reference_count"] += 1
                        details.append(_new_detail(
                            entity_type=entity_type,
                            entity_id=int(entity.id),
                            array_index=index,
                            raw_value=raw_value,
                            migration_batch_id=batch_id,
                            analysis=analysis,
                            result="unresolved",
                        ))
                        continue

                    summary["normalized_reference_count"] += 1
                    unique_by_type[entity_type].add(key)
                    current_target = (entity_type, int(entity.id))
                    if key in ambiguous_keys:
                        if key not in reported_ambiguous_keys:
                            summary["media_reference_conflict_count"] += 1
                            reported_ambiguous_keys.add(key)
                        details.append(_new_detail(
                            entity_type=entity_type,
                            entity_id=int(entity.id),
                            array_index=index,
                            raw_value=raw_value,
                            migration_batch_id=batch_id,
                            analysis=analysis,
                            result="conflict",
                            error_code="media_key_referenced_by_multiple_entities",
                        ))
                        continue
                    if key in blocked_existing_keys:
                        details.append(_new_detail(
                            entity_type=entity_type,
                            entity_id=int(entity.id),
                            array_index=index,
                            raw_value=raw_value,
                            migration_batch_id=batch_id,
                            analysis=analysis,
                            result="conflict",
                            error_code=blocked_existing_keys[key],
                        ))
                        continue
                    prior_target = seen_targets.get(key)
                    if prior_target is not None:
                        if prior_target == current_target:
                            summary["media_reference_alias_count"] += 1
                            result, error = "alias", None
                        else:
                            if key not in reported_ambiguous_keys:
                                summary["media_reference_conflict_count"] += 1
                                reported_ambiguous_keys.add(key)
                            result, error = "conflict", "media_key_referenced_by_multiple_entities"
                        details.append(_new_detail(
                            entity_type=entity_type,
                            entity_id=int(entity.id),
                            array_index=index,
                            raw_value=raw_value,
                            migration_batch_id=batch_id,
                            analysis=analysis,
                            result=result,
                            error_code=error,
                        ))
                        continue
                    seen_targets[key] = current_target

                    if key not in existing_cache:
                        existing_cache[key] = db.query(MediaAssetLifecycle).filter(
                            MediaAssetLifecycle.object_key == key
                        ).first()
                    lifecycle = existing_cache[key]
                    desired_state = _desired_state(entity)
                    if lifecycle is None:
                        summary["missing_before_backfill_key_count"] += 1
                        summary["missing_media_lifecycle_key_count"] += 0 if apply else 1
                        if apply:
                            lifecycle = MediaAssetLifecycle(
                                object_key=key,
                                operation_id=batch_id,
                                owner_userid=entity.owner_userid,
                                entity_type=entity_type,
                                entity_id=entity.id,
                                state=desired_state,
                                draft_expires_at=None,
                                next_attempt_at=now if desired_state == "delete_pending" else None,
                            )
                            db.add(lifecycle)
                            db.flush()
                            existing_cache[key] = lifecycle
                            summary["created_media_lifecycle_count"] += 1
                            summary["matched_media_lifecycle_key_count"] += 1
                            result = "created"
                        else:
                            result = "would_create"
                        details.append(_new_detail(
                            entity_type=entity_type,
                            entity_id=int(entity.id),
                            array_index=index,
                            raw_value=raw_value,
                            migration_batch_id=batch_id,
                            analysis=analysis,
                            result=result,
                        ))
                        if desired_state != "deleted" and entity.deleted_at is not None:
                            summary["non_deleted_soft_deleted_media_key_count"] += 1
                        continue

                    result, error, needs_update = _repair_existing(
                        lifecycle,
                        entity_type=entity_type,
                        entity=entity,
                        desired_state=desired_state,
                        apply=apply,
                        now=now,
                    )
                    if result == "conflict":
                        summary["media_reference_conflict_count"] += 1
                        blocked_existing_keys[key] = error or "media_lifecycle_conflict"
                    else:
                        summary["matched_media_lifecycle_key_count"] += 1
                        if needs_update:
                            summary["repair_required_before_backfill_key_count"] += 1
                            summary["repair_required_media_lifecycle_key_count"] += 0 if apply else 1
                        if result == "updated":
                            summary["updated_media_lifecycle_count"] += 1
                    effective_state = lifecycle.state
                    if entity.deleted_at is not None and effective_state != "deleted":
                        summary["non_deleted_soft_deleted_media_key_count"] += 1
                    details.append(_new_detail(
                        entity_type=entity_type,
                        entity_id=int(entity.id),
                        array_index=index,
                        raw_value=raw_value,
                        migration_batch_id=batch_id,
                        analysis=analysis,
                        result=result,
                        error_code=error,
                    ))

            if apply:
                db.commit()
            last_id = int(rows[-1].id)
            if len(rows) < batch_size:
                break

    _repair_unreferenced_soft_deleted_media(
        db,
        key_targets=key_targets,
        soft_deleted_targets=soft_deleted_targets,
        apply=apply,
        batch_size=batch_size,
        migration_batch_id=batch_id,
        now=now,
        summary=summary,
        details=details,
        blocked_existing_keys=blocked_existing_keys,
    )

    summary["normalized_job_image_key_count"] = len(unique_by_type["job"])
    summary["normalized_resume_image_key_count"] = len(unique_by_type["resume"])
    summary["details"] = details
    return summary


def write_reports(report: dict[str, Any], output_dir: str | Path) -> dict[str, str]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    suffix = report["migration_batch_id"]
    csv_path = output / f"phase10_media_backfill_{suffix}.csv"
    json_path = output / f"phase10_media_backfill_{suffix}.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=DETAIL_FIELDS)
        writer.writeheader()
        writer.writerows(report["details"])
    summary = {key: value for key, value in report.items() if key != "details"}
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"detail_csv": str(csv_path), "summary_json": str(json_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply unambiguous idempotent repairs")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--output-dir", default="phase10-media-backfill-reports")
    args = parser.parse_args()
    with SessionLocal() as db:
        report = backfill_media_lifecycle(db, apply=args.apply, batch_size=args.batch_size)
    paths = write_reports(report, args.output_dir)
    summary = {key: value for key, value in report.items() if key != "details"}
    summary["reports"] = paths
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    blockers = (
        summary["invalid_images_json_count"]
        + summary["unresolved_media_reference_count"]
        + summary["media_reference_conflict_count"]
        + summary["missing_media_lifecycle_key_count"]
        + summary["repair_required_media_lifecycle_key_count"]
    )
    return 0 if blockers == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
