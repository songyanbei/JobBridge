"""命令执行器（Phase 4）。

命令归并 key 与 intent_service._COMMAND_MAP 对齐，
每个命令对应一个 handler，统一接收 (user_ctx, session, args, db) 返回 list[ReplyMessage]。

复用 Phase 3 service：
- /删除我的信息 → user_service.delete_user_data
- /我的状态 → user_service.get_user_status
- /重新找 → conversation_service.reset_search
- /找岗位 /找工人 → conversation_service.set_broker_direction
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import AuditLog, Job, SystemConfig
from app.schemas.conversation import ReplyMessage, SessionState  # noqa: F401
from app.services import conversation_service
from app.services.user_service import UserContext, delete_user_data, get_user_status

# audit_log 可用 action（与 schema.sql 枚举一致）
# 用户主动触发的 /续期 /下架 /招满了 统一归类为 auto_pass，用 reason 区分场景
_AUDIT_ACTION_USER = "auto_pass"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 回复文案常量
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📘 JobBridge 使用指南\n"
    "\n"
    "💬 直接用大白话告诉我您的需求：\n"
    "  · 工人找工作：「苏州找电子厂，5000以上，包吃住」\n"
    "  · 厂家发岗位：「昆山电子厂招普工30人，5500月薪包吃住」\n"
    "  · 中介找岗位：发送 /找岗位 后描述\n"
    "\n"
    "📋 常用指令：\n"
    "  /帮助          查看本指南\n"
    "  /重新找        清空当前搜索条件重新开始\n"
    "  /找岗位        切换到找岗位模式（中介）\n"
    "  /找工人        切换到找工人模式（中介）\n"
    "  /续期 [天数]   延长岗位有效期（默认 15 天）\n"
    "  /下架          下架岗位\n"
    "  /招满了        标记岗位为已招满\n"
    "  /我的状态      查询账号和最近提交状态\n"
    "  /人工客服      转人工客服\n"
    "  /删除我的信息  删除您的资料（工人）"
)

HUMAN_AGENT_TEXT = (
    "已为您转人工客服，请稍候我们会尽快联系您。\n"
    "如紧急可直接拨打客服电话：400-XXX-XXXX"
)

RESET_SEARCH_SUCCESS = "已帮您清空当前搜索条件和结果，可以重新告诉我您的需求。"
RESET_SEARCH_EMPTY = "当前没有可清空的搜索条件。"
# Stage A：/重新找 撞到 pending 上传时的提示（spec §9.7）。
RESET_SEARCH_PENDING_FMT = "搜索条件已重置；您仍在发布{kind}（缺{field_name}），请继续补充或发 /取消 放弃。"

BROKER_ONLY = "只有中介账号可以切换双向模式。"
SWITCH_JOB_OK = "已切换到【找岗位】模式。请告诉我您想找什么样的岗位。"
SWITCH_WORKER_OK = "已切换到【找工人】模式。请告诉我您想招什么样的工人。"

NO_RENEWABLE_JOB = "未找到可续期的岗位。"
NO_DELISTABLE_JOB = "未找到可下架的在线岗位。"
NO_FILLABLE_JOB = "未找到可标记招满的在线岗位。"

UNKNOWN_COMMAND = "暂不支持该指令，输入 /帮助 查看可用命令。"
ROLE_NOT_ALLOWED = "您的账号角色暂无权执行此命令。"


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

def execute(
    command: str,
    args: str,
    user_ctx: UserContext,
    session: SessionState | None,
    db: Session,
) -> list[ReplyMessage]:
    """执行归并后的命令 key。

    Args:
        command: intent_service._COMMAND_MAP 归并后的 key
                 (help / reset_search / switch_to_job / switch_to_worker /
                  renew_job / delist_job / filled_job / delete_my_data /
                  human_agent / my_status)
        args: 命令参数（可能为空）
        user_ctx: 已识别的用户上下文
        session: 当前会话（可能为 None）
        db: DB session

    Returns:
        回复消息列表（通常一条，极少数场景多条）
    """
    handler = _HANDLERS.get(command)
    if handler is None:
        logger.warning("command_service: unknown command key=%s", command)
        return [_reply(user_ctx, UNKNOWN_COMMAND)]
    return handler(args=args, user_ctx=user_ctx, session=session, db=db)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def _handle_help(*, args: str, user_ctx: UserContext, session, db) -> list[ReplyMessage]:
    return [_reply(user_ctx, HELP_TEXT)]


def _handle_human_agent(*, args: str, user_ctx: UserContext, session, db) -> list[ReplyMessage]:
    return [_reply(user_ctx, HUMAN_AGENT_TEXT)]


def _handle_reset_search(
    *, args: str, user_ctx: UserContext, session: SessionState | None, db,
) -> list[ReplyMessage]:
    if session is None or (
        not session.search_criteria
        and session.candidate_snapshot is None
        and not session.shown_items
    ):
        return [_reply(user_ctx, RESET_SEARCH_EMPTY)]

    has_pending = bool(session.pending_upload_intent)
    conversation_service.reset_search(session)
    conversation_service.save_session(user_ctx.external_userid, session)

    if has_pending:
        # Stage A §9.7：pending 草稿仍在编辑时，回复带"仍在发布"的提示文案，
        # 避免用户误以为草稿也被丢了。
        from app.services.upload_service import _FIELD_DISPLAY_NAMES
        kind = "简历" if session.pending_upload_intent == "upload_resume" else "岗位"
        field_name = _FIELD_DISPLAY_NAMES.get(session.awaiting_field, session.awaiting_field or "字段")
        return [_reply(
            user_ctx,
            RESET_SEARCH_PENDING_FMT.format(kind=kind, field_name=field_name),
        )]
    return [_reply(user_ctx, RESET_SEARCH_SUCCESS)]


def _handle_switch_to_job(
    *, args: str, user_ctx: UserContext, session: SessionState | None, db,
) -> list[ReplyMessage]:
    return _switch_broker_direction(user_ctx, session, "search_job", SWITCH_JOB_OK)


def _handle_switch_to_worker(
    *, args: str, user_ctx: UserContext, session: SessionState | None, db,
) -> list[ReplyMessage]:
    return _switch_broker_direction(user_ctx, session, "search_worker", SWITCH_WORKER_OK)


def _switch_broker_direction(
    user_ctx: UserContext,
    session: SessionState | None,
    direction: str,
    success_text: str,
) -> list[ReplyMessage]:
    if user_ctx.role != "broker":
        return [_reply(user_ctx, BROKER_ONLY)]

    if session is None:
        session = conversation_service.create_session(user_ctx.external_userid, user_ctx.role)

    err = conversation_service.set_broker_direction(session, direction)
    if err:
        return [_reply(user_ctx, err)]

    conversation_service.save_session(user_ctx.external_userid, session)
    return [_reply(user_ctx, success_text)]


def _handle_my_status(
    *, args: str, user_ctx: UserContext, session, db: Session,
) -> list[ReplyMessage]:
    info = get_user_status(user_ctx.external_userid, db)
    if not info.get("found"):
        return [_reply(user_ctx, info.get("message", "未找到您的账号记录。"))]

    lines = [f"📇 账号状态：{_status_display(info['status'])}"]
    role_display = {"worker": "工人", "factory": "厂家", "broker": "中介"}.get(
        info.get("role", ""), info.get("role", "")
    )
    if role_display:
        lines.append(f"🧾 账号角色：{role_display}")

    if info.get("registered_at"):
        lines.append(f"🗓 注册时间：{info['registered_at']}")

    latest_job = info.get("latest_job")
    if latest_job:
        lines.append(
            f"💼 最近岗位：#{latest_job['id']} "
            f"审核状态 {_audit_display(latest_job['audit_status'])}"
        )

    latest_resume = info.get("latest_resume")
    if latest_resume:
        lines.append(
            f"📄 最近简历：#{latest_resume['id']} "
            f"审核状态 {_audit_display(latest_resume['audit_status'])}"
        )

    return [_reply(user_ctx, "\n".join(lines))]


def _handle_delete_my_data(
    *, args: str, user_ctx: UserContext, session, db: Session,
) -> list[ReplyMessage]:
    # 按方案设计 §17.2.4：一期单步触发，不加二次确认；强制只对工人开放
    if user_ctx.role != "worker":
        return [_reply(user_ctx, "该命令仅对工人账号开放。")]
    reply_text = delete_user_data(user_ctx.external_userid, db)
    return [_reply(user_ctx, reply_text)]


# ---------------------------------------------------------------------------
# /续期 /下架 /招满了（厂家/中介）
# ---------------------------------------------------------------------------

_ALLOWED_RENEW_DAYS = frozenset({15, 30})
_DEFAULT_RENEW_DAYS = 15


def _handle_renew_job(
    *, args: str, user_ctx: UserContext, session, db: Session,
) -> list[ReplyMessage]:
    if user_ctx.role not in ("factory", "broker"):
        return [_reply(user_ctx, ROLE_NOT_ALLOWED)]

    days = _parse_renew_days(args)
    if days is None:
        return [_reply(
            user_ctx,
            "续期天数仅支持 15 或 30 天，例如 /续期 15 或 /续期 30。",
        )]

    now = datetime.now(timezone.utc)
    jobs = db.query(Job).filter(
        Job.owner_userid == user_ctx.external_userid,
        Job.deleted_at.is_(None),
        Job.delist_reason.is_(None),
        Job.expires_at > now,
    ).order_by(Job.created_at.desc()).all()

    if not jobs:
        return [_reply(user_ctx, NO_RENEWABLE_JOB)]

    # 多岗位且无参数 → 返回列表让用户确认（phase4-main §3.1 模块 D 细则）
    if len(jobs) > 1 and not args.strip():
        return [_reply(user_ctx, _render_renew_list(jobs))]

    # 单岗位 或 多岗位+明确天数 → 对最近一条执行续期
    target = jobs[0]
    ttl_cap = _renew_ttl_cap_days(db)

    # TTL 上限：从 now 起算（避免老岗位续期反而缩短）
    max_expires = now + timedelta(days=ttl_cap)
    new_expires = target.expires_at + timedelta(days=days)
    capped = False
    if new_expires > max_expires:
        # 取两者较大值，保证续完不少于原 expires
        new_expires = max(max_expires, target.expires_at)
        capped = new_expires < target.expires_at + timedelta(days=days)

    target.expires_at = new_expires
    db.flush()
    _write_audit_log(
        db,
        target_type="job",
        target_id=target.id,
        action=_AUDIT_ACTION_USER,
        reason=f"user_renew_job days={days} capped={capped}",
        operator=user_ctx.external_userid,
        snapshot={"days": days, "new_expires_at": new_expires.isoformat(), "capped": capped},
    )

    title = f"#{target.id} {target.city}·{target.job_category}"
    msg = f"已为您将岗位【{title}】续期 {days} 天。"
    if capped:
        msg += "（已达 TTL 上限，自动截断到允许的最大值）"

    if len(jobs) > 1:
        msg += (
            f"\n您名下还有 {len(jobs) - 1} 个在线岗位，本次仅续期了最近发布的这一条；"
            f"若需续期指定岗位，可发送 /我的状态 查看后，再次发送 /续期 {days}。"
        )

    return [_reply(user_ctx, msg)]


def _render_renew_list(jobs) -> str:
    """多岗位时的列表提示文案。"""
    lines = ["您名下有多个可续期的岗位，请指定要续期的岗位：", ""]
    markers = ["①", "②", "③", "④", "⑤"]
    for i, job in enumerate(jobs[:5]):
        marker = markers[i] if i < len(markers) else f"({i+1})"
        remain = _days_remaining(job.expires_at)
        lines.append(
            f"{marker} #{job.id} {job.city}·{job.job_category}（剩 {remain} 天）"
        )
    if len(jobs) > 5:
        lines.append(f"  ……还有 {len(jobs) - 5} 条未列出")
    lines.append("")
    lines.append('请回复"/续期 15"或"/续期 30"续期最近一条，或联系客服指定岗位续期。')
    return "\n".join(lines)


def _days_remaining(expires_at) -> int:
    if expires_at is None:
        return 0
    now = datetime.now(timezone.utc)
    # expires_at 可能是 naive datetime（由 ORM 直接返回 MySQL UTC datetime）
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    delta = expires_at - now
    return max(0, delta.days)


def _handle_delist_job(
    *, args: str, user_ctx: UserContext, session, db: Session,
) -> list[ReplyMessage]:
    return _delist_common(
        user_ctx, db,
        delist_reason="manual_delist",
        empty_text=NO_DELISTABLE_JOB,
        action_verb="下架",
    )


def _handle_filled_job(
    *, args: str, user_ctx: UserContext, session, db: Session,
) -> list[ReplyMessage]:
    return _delist_common(
        user_ctx, db,
        delist_reason="filled",
        empty_text=NO_FILLABLE_JOB,
        action_verb="标记为招满",
    )


def _delist_common(
    user_ctx: UserContext,
    db: Session,
    *,
    delist_reason: str,
    empty_text: str,
    action_verb: str,
) -> list[ReplyMessage]:
    if user_ctx.role not in ("factory", "broker"):
        return [_reply(user_ctx, ROLE_NOT_ALLOWED)]

    now = datetime.now(timezone.utc)
    jobs = db.query(Job).filter(
        Job.owner_userid == user_ctx.external_userid,
        Job.deleted_at.is_(None),
        Job.delist_reason.is_(None),
        Job.expires_at > now,
    ).order_by(Job.created_at.desc()).all()

    if not jobs:
        return [_reply(user_ctx, empty_text)]

    target = jobs[0]
    target.delist_reason = delist_reason
    db.flush()

    _write_audit_log(
        db,
        target_type="job",
        target_id=target.id,
        action=_AUDIT_ACTION_USER,
        reason=(
            "user_delist_job"
            if delist_reason == "manual_delist"
            else "user_filled_job"
        ),
        operator=user_ctx.external_userid,
        snapshot={"delist_reason": delist_reason},
    )

    title = f"#{target.id} {target.city}·{target.job_category}"
    msg = f"已将岗位【{title}】{action_verb}。"
    if len(jobs) > 1:
        msg += f"\n您名下还有 {len(jobs) - 1} 个在线岗位，本次操作仅处理了最近发布的这一条。"
    return [_reply(user_ctx, msg)]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

_DAYS_PATTERN = re.compile(r"(\d+)")


def _parse_renew_days(args: str) -> int | None:
    """解析续期参数。

    约束：
    - 空 → 默认 15 天
    - 含数字 → 仅接受 15 或 30
    - 其他格式 → None（调用方回复无效）
    """
    if not args:
        return _DEFAULT_RENEW_DAYS
    m = _DAYS_PATTERN.search(args)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except (ValueError, TypeError):
        return None
    if n in _ALLOWED_RENEW_DAYS:
        return n
    return None


def _renew_ttl_cap_days(db: Session) -> int:
    """从 system_config 读取 ttl.job.days，续期上限取 2 倍。"""
    cfg = db.query(SystemConfig).filter(
        SystemConfig.config_key == "ttl.job.days",
    ).first()
    base = 30
    if cfg:
        try:
            base = int(cfg.config_value)
        except (ValueError, TypeError):
            pass
    return base * 2


def _write_audit_log(
    db: Session,
    *,
    target_type: str,
    target_id,
    action: str,
    reason: str,
    operator: str,
    snapshot: dict | None = None,
) -> None:
    db.add(AuditLog(
        target_type=target_type,
        target_id=str(target_id),
        action=action,
        reason=reason,
        operator=operator,
        snapshot=snapshot,
    ))


def _reply(user_ctx: UserContext, content: str) -> ReplyMessage:
    return ReplyMessage(userid=user_ctx.external_userid, content=content)


def _status_display(status: str) -> str:
    return {"active": "正常", "blocked": "已封禁", "deleted": "已删除"}.get(status, status)


def _audit_display(status: str) -> str:
    return {"passed": "已通过", "pending": "待审核", "rejected": "未通过"}.get(status, status)


_HANDLERS = {
    "help": _handle_help,
    "reset_search": _handle_reset_search,
    "switch_to_job": _handle_switch_to_job,
    "switch_to_worker": _handle_switch_to_worker,
    "renew_job": _handle_renew_job,
    "delist_job": _handle_delist_job,
    "filled_job": _handle_filled_job,
    "delete_my_data": _handle_delete_my_data,
    "human_agent": _handle_human_agent,
    "my_status": _handle_my_status,
}
