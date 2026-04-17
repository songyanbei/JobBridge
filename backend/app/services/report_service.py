"""数据看板 service（Phase 5 模块 H）。

指标源：
- DAU：user.last_active_at
- 上传：job.created_at / resume.created_at
- 检索次数：conversation_log intent in ('search_job','search_worker','show_more','follow_up')
- 命中率：conversation_log direction='out' 的 criteria_snapshot.recommend_count>0 占比
- 空召回：criteria_snapshot.recommend_count==0 占比
- 待审：job + resume audit_status='pending'
- 详情点击：event_log.count

性能要求：缓存 60 秒，单次查询 < 500ms（缓存外）。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.core.redis_client import get_redis
from app.models import (
    ConversationLog,
    EventLog,
    Job,
    Resume,
    SystemConfig,
    User,
)

logger = logging.getLogger(__name__)

DASHBOARD_CACHE_KEY = "report_cache:dashboard"
SEARCH_INTENTS = ("search_job", "search_worker", "show_more", "follow_up")


def _config_int(db: Session, key: str, default: int) -> int:
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    try:
        return int(cfg.config_value) if cfg else default
    except (TypeError, ValueError):
        return default


def _day_range(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min)
    end = start + timedelta(days=1)
    return start, end


# ---------------------------------------------------------------------------
# 核心：计算单日指标
# ---------------------------------------------------------------------------

def _current_audit_pending(db: Session) -> int:
    """当前时刻的待审积压总数。不可按历史日期计算。"""
    return int(
        (db.query(func.count(Job.id)).filter(Job.audit_status == "pending", Job.deleted_at.is_(None)).scalar() or 0)
        + (db.query(func.count(Resume.id)).filter(Resume.audit_status == "pending", Resume.deleted_at.is_(None)).scalar() or 0)
    )


def _calc_day(db: Session, d: date, include_audit_pending: bool = False) -> dict:
    """聚合单日指标。

    audit_pending 只在 `include_audit_pending=True`（即调用方语义为"今日"时）才附带，
    历史日无法回溯"当时的积压数"，因此其它日期不返回该字段，避免误导。
    """
    start, end = _day_range(d)

    dau_rows = db.query(User.role, func.count(User.external_userid)).filter(
        User.last_active_at >= start, User.last_active_at < end,
    ).group_by(User.role).all()
    dau_worker = dau_factory = dau_broker = 0
    for role, cnt in dau_rows:
        if role == "worker":
            dau_worker = cnt
        elif role == "factory":
            dau_factory = cnt
        elif role == "broker":
            dau_broker = cnt
    dau_total = dau_worker + dau_factory + dau_broker

    uploads_job = db.query(func.count(Job.id)).filter(
        Job.created_at >= start, Job.created_at < end,
    ).scalar() or 0
    uploads_resume = db.query(func.count(Resume.id)).filter(
        Resume.created_at >= start, Resume.created_at < end,
    ).scalar() or 0

    search_count = db.query(func.count(ConversationLog.id)).filter(
        ConversationLog.created_at >= start, ConversationLog.created_at < end,
        ConversationLog.direction == "in",
        ConversationLog.intent.in_(SEARCH_INTENTS),
    ).scalar() or 0

    # 命中率 / 空召回率：基于系统回复日志
    out_rows = db.query(ConversationLog.criteria_snapshot).filter(
        ConversationLog.created_at >= start, ConversationLog.created_at < end,
        ConversationLog.direction == "out",
        ConversationLog.intent.in_(SEARCH_INTENTS),
    ).all()
    hit_count = 0
    empty_count = 0
    total_out = 0
    for (snap,) in out_rows:
        total_out += 1
        if not snap:
            empty_count += 1
            continue
        # snap 可能是 dict 或 JSON str
        data = snap if isinstance(snap, dict) else _safe_json(snap)
        count = 0
        if isinstance(data, dict):
            count = int(data.get("recommend_count") or 0)
        if count > 0:
            hit_count += 1
        else:
            empty_count += 1
    hit_rate = round(hit_count / total_out, 4) if total_out else 0.0
    empty_recall_rate = round(empty_count / total_out, 4) if total_out else 0.0

    metrics = {
        "date": d.isoformat(),
        "dau_total": int(dau_total),
        "dau_worker": int(dau_worker),
        "dau_factory": int(dau_factory),
        "dau_broker": int(dau_broker),
        "uploads_job": int(uploads_job),
        "uploads_resume": int(uploads_resume),
        "search_count": int(search_count),
        "hit_rate": hit_rate,
        "empty_recall_rate": empty_recall_rate,
    }
    if include_audit_pending:
        metrics["audit_pending"] = _current_audit_pending(db)
    return metrics


def _safe_json(value: Any) -> dict | None:
    try:
        return json.loads(value)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dashboard（含缓存）
# ---------------------------------------------------------------------------

def get_dashboard(db: Session, force_refresh: bool = False) -> dict:
    r = None
    cache_ttl = _config_int(db, "report.cache_ttl_seconds", 60)
    try:
        r = get_redis()
        if not force_refresh:
            cached = r.get(DASHBOARD_CACHE_KEY)
            if cached:
                return json.loads(cached)
    except Exception:
        logger.exception("report dashboard: redis cache read failed (fallback to DB)")

    today = date.today()
    yesterday = today - timedelta(days=1)
    data = {
        # 只有 today 才附带 audit_pending（当前时刻积压数，不可按历史日回溯）
        "today": _calc_day(db, today, include_audit_pending=True),
        "yesterday": _calc_day(db, yesterday, include_audit_pending=False),
        "trend_7d": [
            _calc_day(db, today - timedelta(days=i), include_audit_pending=False)
            for i in range(6, -1, -1)
        ],
    }

    try:
        if r is not None:
            r.setex(DASHBOARD_CACHE_KEY, cache_ttl, json.dumps(data, ensure_ascii=False, default=str))
    except Exception:
        logger.exception("report dashboard: redis cache write failed")
    return data


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

def get_trends(db: Session, range_str: str, frm: date | None, to: date | None) -> dict:
    if range_str == "7d":
        end = date.today()
        start = end - timedelta(days=6)
    elif range_str == "30d":
        end = date.today()
        start = end - timedelta(days=29)
    elif range_str == "custom":
        if not frm or not to:
            raise BusinessException(40101, "custom range 需要 from/to")
        if (to - frm).days > 90:
            raise BusinessException(40101, "时间范围不能超过 90 天")
        start, end = frm, to
    else:
        raise BusinessException(40101, "无效的 range")

    days = []
    cur = start
    while cur <= end:
        days.append(_calc_day(db, cur))
        cur += timedelta(days=1)
    return {"range": range_str, "from": start.isoformat(), "to": end.isoformat(), "days": days}


# ---------------------------------------------------------------------------
# TOP
# ---------------------------------------------------------------------------

def get_top(db: Session, dim: str, limit: int = 10) -> list[dict]:
    limit = max(1, min(limit, 50))
    today = date.today()
    start = datetime.combine(today - timedelta(days=29), time.min)

    if dim == "city":
        rows = db.query(Job.city, func.count(Job.id)).filter(
            Job.created_at >= start, Job.deleted_at.is_(None),
        ).group_by(Job.city).order_by(func.count(Job.id).desc()).limit(limit).all()
        return [{"key": k or "未知", "count": int(c)} for k, c in rows]
    if dim == "job_category":
        rows = db.query(Job.job_category, func.count(Job.id)).filter(
            Job.created_at >= start, Job.deleted_at.is_(None),
        ).group_by(Job.job_category).order_by(func.count(Job.id).desc()).limit(limit).all()
        return [{"key": k or "未知", "count": int(c)} for k, c in rows]
    if dim == "role":
        rows = db.query(User.role, func.count(User.external_userid)).filter(
            User.last_active_at >= start,
        ).group_by(User.role).order_by(func.count(User.external_userid).desc()).limit(limit).all()
        return [{"key": k, "count": int(c)} for k, c in rows]
    raise BusinessException(40101, "无效的 dim")


# ---------------------------------------------------------------------------
# Funnel（近 30 天）
# ---------------------------------------------------------------------------

def get_funnel(db: Session) -> list[dict]:
    today = date.today()
    start = datetime.combine(today - timedelta(days=29), time.min)

    registered = db.query(func.count(User.external_userid)).filter(User.registered_at >= start).scalar() or 0

    first_msg_users = db.query(func.count(func.distinct(ConversationLog.userid))).filter(
        ConversationLog.created_at >= start, ConversationLog.direction == "in",
    ).scalar() or 0

    search_users = db.query(func.count(func.distinct(ConversationLog.userid))).filter(
        ConversationLog.created_at >= start,
        ConversationLog.direction == "in",
        ConversationLog.intent.in_(SEARCH_INTENTS),
    ).scalar() or 0

    # 收到推荐：direction=out + 含推荐结果
    out_logs = db.query(ConversationLog.userid, ConversationLog.criteria_snapshot).filter(
        ConversationLog.created_at >= start,
        ConversationLog.direction == "out",
        ConversationLog.intent.in_(SEARCH_INTENTS),
    ).all()
    recommend_user_set: set[str] = set()
    for userid, snap in out_logs:
        data = snap if isinstance(snap, dict) else _safe_json(snap)
        if isinstance(data, dict) and int(data.get("recommend_count") or 0) > 0:
            recommend_user_set.add(userid)
    recommend_users = len(recommend_user_set)

    click_users = db.query(func.count(func.distinct(EventLog.userid))).filter(
        EventLog.occurred_at >= start,
        EventLog.event_type == "miniprogram_click",
    ).scalar() or 0

    return [
        {"stage": "注册", "count": int(registered)},
        {"stage": "首次发消息", "count": int(first_msg_users)},
        {"stage": "首次有效检索", "count": int(search_users)},
        {"stage": "收到推荐", "count": int(recommend_users)},
        {"stage": "点详情", "count": int(click_users)},
    ]


# ---------------------------------------------------------------------------
# Export 数据行（供 api 层打包 CSV）
# ---------------------------------------------------------------------------

def export_metric(db: Session, metric: str, frm: date, to: date) -> tuple[list[str], list[list]]:
    if metric == "daily":
        # audit_pending 只记录当前时刻值，不是历史日期的合法指标——不导出到 daily CSV
        headers = [
            "date", "dau_total", "dau_worker", "dau_factory", "dau_broker",
            "uploads_job", "uploads_resume", "search_count",
            "hit_rate", "empty_recall_rate",
        ]
        rows: list[list] = []
        cur = frm
        while cur <= to:
            m = _calc_day(db, cur)
            rows.append([m[h] for h in headers])
            cur += timedelta(days=1)
        return headers, rows
    raise BusinessException(40101, "不支持的 metric")
