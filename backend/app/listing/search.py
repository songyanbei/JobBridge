"""Worker -> recruitment.job search facade.

The facade delegates querying, ranking, visibility and snapshot management to
the existing ``search_service``. It owns only the stable public card projection
and the feature-gated compatibility boundary.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.schemas.conversation import SessionState
from app.schemas.search import ListingCard, SearchCriteriaPatch
from app.services.search_permission import check_search_permission
from app.services.user_service import UserContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchTurn:
    raw_query: str = ""
    user_msg_id: str | None = None


@dataclass
class FacadeResult:
    result: Any
    outcome: Any
    cards: list[ListingCard]
    used_facade: bool = True
    fallback_reason: str | None = None

    @property
    def reply_text(self) -> str:
        return str(getattr(self.result, "reply_text", ""))


def _turn_values(turn: SearchTurn | dict | Any) -> tuple[str, str | None]:
    if isinstance(turn, dict):
        return str(turn.get("raw_query") or turn.get("content") or ""), turn.get("user_msg_id") or turn.get("msg_id")
    return str(getattr(turn, "raw_query", getattr(turn, "content", "")) or ""), getattr(turn, "user_msg_id", getattr(turn, "msg_id", None))


def apply_criteria_patch(criteria: dict, patches: list[SearchCriteriaPatch | dict]) -> dict:
    """Apply only the explicit add/update/remove patch contract."""
    current = dict(criteria or {})
    for raw in patches or []:
        patch = raw if isinstance(raw, SearchCriteriaPatch) else SearchCriteriaPatch.model_validate(raw)
        if patch.op == "update":
            current[patch.field] = patch.value
        elif patch.op == "remove":
            if patch.value is None:
                current.pop(patch.field, None)
            elif isinstance(current.get(patch.field), list):
                remaining = [item for item in current[patch.field] if item != patch.value]
                if remaining:
                    current[patch.field] = remaining
                else:
                    current.pop(patch.field, None)
            else:
                current.pop(patch.field, None)
        else:
            existing = current.get(patch.field)
            values = patch.value if isinstance(patch.value, list) else [patch.value]
            if not isinstance(existing, list):
                existing = [] if existing is None else [existing]
            current[patch.field] = existing + [value for value in values if value not in existing]
    return current


def _opaque_contact_id(actor: UserContext, listing_ref: str, snapshot_id: str | None) -> str:
    salt = getattr(settings, "app_secret_key", "") or secrets.token_hex(16)
    payload = f"{salt}:{actor.external_userid}:{listing_ref}:{snapshot_id or ''}".encode()
    return "cr_" + hashlib.sha256(payload).hexdigest()[:32]


def _salary_text(job: dict) -> str | None:
    floor, ceiling = job.get("salary_floor_monthly"), job.get("salary_ceiling_monthly")
    if floor is not None and ceiling is not None and ceiling > floor:
        return f"{floor}-{ceiling}元/月"
    if floor is not None:
        return f"{floor}元/月"
    if ceiling is not None:
        return f"最高{ceiling}元/月"
    return None


def _job_card(job: dict, actor: UserContext, snapshot_id: str | None, explanation: str | None = None) -> ListingCard:
    listing_ref = f"recruitment.job:{job.get('id')}"
    company = job.get("hiring_company") or job.get("company")
    category = job.get("job_category")
    title = " | ".join(str(value) for value in (company, category) if value) or "岗位"
    city, district = job.get("city"), job.get("district")
    location = "".join(str(value) for value in (city, district) if value) or None
    benefits = [label for key, label in (("provide_meal", "包吃"), ("provide_housing", "包住")) if job.get(key)]
    attrs = {key: value for key, value in {
        "salary": _salary_text(job), "pay_type": job.get("pay_type"),
        "shift_pattern": job.get("shift_pattern"), "work_hours": job.get("work_hours"),
        "benefits": benefits or None, "headcount": job.get("headcount"),
        "employment_type": job.get("employment_type"),
    }.items() if value not in (None, "", [])}
    contact_id = _opaque_contact_id(actor, listing_ref, snapshot_id)
    return ListingCard(
        listing_id=listing_ref,
        listing_ref=listing_ref,
        title=title,
        body_summary=str(job.get("description") or "").strip()[:300],
        location_text=location,
        attributes=attrs,
        contact_action="回复“联系”获取进一步沟通入口",
        contact_request_id=contact_id,
        explanation=explanation,
    )


class JobSearchFacade:
    profile = "recruitment.job"
    version = "job-search-facade.v1"

    def __init__(self, db=None, *, enabled: bool | None = None, legacy_service=None):
        self.db = db
        self.enabled = bool(getattr(settings, "job_search_facade_enabled", False)) if enabled is None else bool(enabled)
        self.legacy_service = legacy_service

    def _legacy_search(self, actor, criteria, session, turn, db):
        service = self.legacy_service
        if service is None:
            from app.services import search_service as service
        raw_query, msg_id = _turn_values(turn)
        return service.search_jobs(criteria, raw_query, session, actor, db, user_msg_id=msg_id)

    def search_jobs_v1(self, actor: UserContext, criteria: dict, session: SessionState, turn: SearchTurn | dict | Any, db=None) -> FacadeResult:
        db = db or self.db
        if actor.role != "worker" or session.profile != self.profile:
            raise PermissionError("job search facade requires worker/recruitment.job")
        if db is None:
            raise ValueError("db is required")
        _, msg_id = _turn_values(turn)
        decision = check_search_permission(actor, "search_job", entrypoint="listing.search", request_id=msg_id)
        if not decision.allowed:
            from app.services.search_permission import denied_search_response
            result, outcome = denied_search_response(decision)
            return FacadeResult(result, outcome, [], used_facade=False, fallback_reason=decision.reason_code)
        try:
            result, outcome = self._legacy_search(actor, dict(criteria or {}), session, turn, db)
            if not self.enabled:
                return FacadeResult(result, outcome, [], used_facade=False, fallback_reason="disabled")
            cards = self.cards_for_snapshot(actor, session, db, result)
            return FacadeResult(result, outcome, cards)
        except Exception as exc:
            logger.exception("job search facade failed; caller should use legacy", exc_info=True)
            raise RuntimeError("job_search_facade_failed") from exc

    search = search_jobs_v1

    def cards_for_snapshot(self, actor, session, db, result) -> list[ListingCard]:
        from app.services import permission_service, search_service
        snapshot = session.candidate_snapshot
        if snapshot is None:
            return []
        count = int(getattr(result, "result_count", 0) or 0)
        ids = list(session.shown_items or [])[-count:] if count else []
        jobs = search_service._validate_job_ids(ids, db)
        dicts = search_service._jobs_to_dicts(jobs, db)
        visibility = search_service._visibility_snapshot(db, "search_job", actor.role)
        visible = permission_service.filter_jobs_batch(dicts, actor.role, visibility)
        return [_job_card(job, actor, snapshot.snapshot_id) for job in visible]

    _cards_from_snapshot = cards_for_snapshot


def search_jobs_v1(actor, criteria, session, turn, db=None, **kwargs) -> FacadeResult:
    return JobSearchFacade(db, **kwargs).search_jobs_v1(actor, criteria, session, turn, db=db)


__all__ = ["FacadeResult", "JobSearchFacade", "SearchTurn", "apply_criteria_patch", "search_jobs_v1"]
