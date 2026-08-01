"""Unified role and account-switch authorization for recommendation searches."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from app.schemas.search import SearchOutcome, SearchResult
from app.services.visibility_contract import (
    ROLE_SCENE_ACCESS,
    ViewerRole,
    VisibilityScene,
)

logger = logging.getLogger(__name__)

SearchDirection = Literal["search_job", "search_worker"]

_SCENE_BY_DIRECTION: dict[SearchDirection, VisibilityScene] = {
    "search_job": VisibilityScene.JOB_SEARCH,
    "search_worker": VisibilityScene.CANDIDATE_SEARCH,
}


class ResolvedSearchDirection(str):
    """String-compatible routing result with a controlled unsupported marker."""

    supported: bool
    reason_code: str | None

    def __new__(
        cls,
        direction: SearchDirection,
        *,
        supported: bool = True,
        reason_code: str | None = None,
    ):
        value = super().__new__(cls, direction)
        value.supported = supported
        value.reason_code = reason_code
        return value


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    role: str
    direction: str
    role_allowed: bool
    account_switch: str
    account_allowed: bool
    reason_code: str
    entrypoint: str
    request_id: str | None = None

    def matches(self, user_ctx, direction: str) -> bool:
        """Whether this decision can safely be reused by a downstream entry."""

        if self.role != str(getattr(user_ctx, "role", "")) or self.direction != direction:
            return False
        scene = _SCENE_BY_DIRECTION.get(direction)
        try:
            role = ViewerRole(self.role)
        except ValueError:
            role = None
        expected_role_allowed = bool(
            scene is not None
            and role is not None
            and role in ROLE_SCENE_ACCESS[scene]
        )
        current_account_value = bool(getattr(user_ctx, self.account_switch, False))
        expected_allowed = expected_role_allowed and current_account_value
        return (
            self.role_allowed == expected_role_allowed
            and self.account_allowed == current_account_value
            and self.allowed == expected_allowed
        )


def check_search_permission(
    user_ctx,
    direction: str,
    *,
    entrypoint: str = "unknown",
    request_id: str | None = None,
) -> PermissionDecision:
    """Check the immutable role gate and the mutable per-account switch.

    Only explicitly safe scalar facts are logged.  The user context object and
    user identifiers are intentionally never serialized.
    """

    role_text = str(getattr(user_ctx, "role", ""))
    account_switch = (
        "can_search_jobs" if direction == "search_job" else "can_search_workers"
    )
    account_allowed = bool(getattr(user_ctx, account_switch, False))

    if direction not in _SCENE_BY_DIRECTION:
        role_allowed = False
        reason_code = "invalid_direction"
        account_switch = "none"
        account_allowed = False
    else:
        try:
            role = ViewerRole(role_text)
        except ValueError:
            role = None
        role_allowed = bool(
            role is not None and role in ROLE_SCENE_ACCESS[_SCENE_BY_DIRECTION[direction]]
        )
        if role is None:
            reason_code = "unknown_role"
        elif not role_allowed:
            reason_code = "role_direction_forbidden"
        elif not account_allowed:
            reason_code = "account_search_disabled"
        else:
            reason_code = "allowed"

    decision = PermissionDecision(
        allowed=role_allowed and account_allowed,
        role=role_text,
        direction=direction,
        role_allowed=role_allowed,
        account_switch=account_switch,
        account_allowed=account_allowed,
        reason_code=reason_code,
        entrypoint=entrypoint,
        request_id=request_id,
    )
    logger.info(
        "search_permission role=%s direction=%s role_allowed=%s "
        "account_switch=%s account_allowed=%s reason_code=%s entrypoint=%s request_id=%s",
        decision.role,
        decision.direction,
        decision.role_allowed,
        decision.account_switch,
        decision.account_allowed,
        decision.reason_code,
        decision.entrypoint,
        decision.request_id or "",
    )
    return decision


def ensure_search_permission(
    user_ctx,
    direction: SearchDirection,
    decision: PermissionDecision | None,
    *,
    entrypoint: str,
    request_id: str | None = None,
) -> PermissionDecision:
    """Reuse a matching upstream decision or defensively recompute it."""

    if decision is not None and decision.matches(user_ctx, direction):
        return decision
    return check_search_permission(
        user_ctx, direction, entrypoint=entrypoint, request_id=request_id,
    )


def denied_search_response(
    decision: PermissionDecision,
) -> tuple[SearchResult, SearchOutcome]:
    """Return the stable tuple contract without touching search/session state."""

    direction: SearchDirection = (
        "search_worker" if decision.direction == "search_worker" else "search_job"
    )
    if decision.reason_code == "role_direction_forbidden":
        if decision.role == "factory" and direction == "search_job":
            reply = "厂家账号不支持搜索岗位。"
        else:
            reply = "当前角色不支持该搜索方向。"
    elif decision.reason_code == "account_search_disabled":
        target = "岗位" if direction == "search_job" else "求职者"
        reply = f"当前账号未开通{target}搜索权限，请联系管理员。"
    else:
        reply = "当前请求不支持该搜索方向。"

    return (
        SearchResult(reply_text=reply, has_more=False, result_count=0),
        SearchOutcome(
            direction=direction,
            criteria_used={},
            initial_count=0,
            final_count=0,
            desired_count=0,
            low_recall_threshold=0,
            visible_count=0,
            shown_count=0,
            remaining_count_capped=0,
            has_more=False,
        ),
    )
