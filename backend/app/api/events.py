"""小程序点击等外部事件回传（Phase 5 模块 J，v0.7 §9.9）。

路径：POST /api/events/miniprogram_click
鉴权：X-Event-Api-Key（不走 JWT）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_event_api_key
from app.core.exceptions import BusinessException
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
    try:
        result = event_service.record_click(
            db,
            userid=payload.userid,
            target_type=payload.target_type,
            target_id=payload.target_id,
            timestamp=payload.timestamp,
            delivery_id=payload.delivery_id,
            request_id=payload.request_id,
            snapshot_id=payload.snapshot_id,
            position=payload.position,
            client_event_id=payload.client_event_id,
        )
    except event_service.ClickAttributionRejected as exc:
        # §9.9：上下文不匹配返回业务错误；service 已写 rejected 行和安全日志
        raise BusinessException(42201, str(exc), {"reason": exc.reason}) from exc
    except ValueError as exc:
        raise BusinessException(42201, str(exc)) from exc
    return ok({
        "deduped": result.deduped,
        "attribution_status": result.attribution_status,
    })
