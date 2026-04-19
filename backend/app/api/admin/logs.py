"""对话日志路由（Phase 5 模块 I）。"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_password_changed as require_admin
from app.core.csv_export import rows_to_csv_bytes
from app.core.responses import paged
from app.models import AdminUser
from app.schemas.conversation import ConversationLogRead
from app.services import log_service

router = APIRouter(prefix="/admin/logs", tags=["admin-logs"])


@router.get("/conversations", summary="对话日志查询（必须带 userid 与时间范围）")
def list_conversations(
    userid: str = Query(..., min_length=1),
    start: datetime = Query(...),
    end: datetime = Query(...),
    direction: str | None = None,
    intent: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows, total = log_service.list_conversations(
        db, userid, start, end, direction, intent, page, size,
    )
    items = [ConversationLogRead.model_validate(r).model_dump(mode="json") for r in rows]
    return paged(items, total, page, size)


@router.get("/conversations/export", summary="对话日志导出 CSV")
def export_conversations(
    userid: str = Query(..., min_length=1),
    start: datetime = Query(...),
    end: datetime = Query(...),
    direction: str | None = None,
    intent: str | None = None,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows = log_service.export_conversations(db, userid, start, end, direction, intent)
    headers = [
        "id", "userid", "direction", "msg_type", "content",
        "wecom_msg_id", "intent", "criteria_snapshot",
        "created_at",
    ]
    body = []
    for r in rows:
        body.append([
            r.id, r.userid, r.direction, r.msg_type, r.content,
            r.wecom_msg_id, r.intent,
            json.dumps(r.criteria_snapshot, ensure_ascii=False) if r.criteria_snapshot else "",
            r.created_at,
        ])
    data = rows_to_csv_bytes(headers, body)
    ts = datetime.now().strftime("%Y%m%d%H%M")
    filename = f"conversations_{userid}_{ts}.csv"
    return Response(content=data, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
    })
