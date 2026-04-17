"""小程序点击等外部事件回传（Phase 5 模块 J）。

路径：POST /api/events/miniprogram_click
鉴权：X-Event-Api-Key（不走 JWT）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_event_api_key
from app.core.responses import ok
from app.schemas.event import MiniProgramClickRequest
from app.services import event_service

router = APIRouter(prefix="/api/events", tags=["events"])


@router.post("/miniprogram_click", summary="小程序点击回传")
def miniprogram_click(
    payload: MiniProgramClickRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_event_api_key),
):
    deduped = event_service.record_click(
        db,
        userid=payload.userid,
        target_type=payload.target_type,
        target_id=payload.target_id,
        timestamp=payload.timestamp,
    )
    return ok({"deduped": deduped})
