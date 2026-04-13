"""用户服务（Phase 3）。

用户识别、自动注册、状态拦截、欢迎判定、删除编排、/我的状态。
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import UserBlocked
from app.models import AuditLog, ConversationLog, Job, Resume, User
from app.services import conversation_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# UserContext — 返回给调用方的用户上下文
# ---------------------------------------------------------------------------

@dataclass
class UserContext:
    external_userid: str
    role: str
    status: str
    display_name: str | None
    company: str | None
    contact_person: str | None
    phone: str | None
    can_search_jobs: bool
    can_search_workers: bool
    is_first_touch: bool
    should_welcome: bool


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def identify_or_register(
    external_userid: str,
    db: Session,
) -> UserContext:
    """识别用户，未注册则自动注册为 worker。

    返回 UserContext，调用方根据 status 和 should_welcome 决定后续行为。
    """
    user = db.query(User).filter(
        User.external_userid == external_userid,
    ).first()

    if user is None:
        # 未预注册用户 → 默认 worker
        user = User(
            external_userid=external_userid,
            role="worker",
            status="active",
            can_search_jobs=True,
            can_search_workers=False,
        )
        db.add(user)
        db.flush()
        logger.info("user_service: auto-registered worker %s", external_userid)
        return UserContext(
            external_userid=external_userid,
            role="worker",
            status="active",
            display_name=None,
            company=None,
            contact_person=None,
            phone=None,
            can_search_jobs=True,
            can_search_workers=False,
            is_first_touch=True,
            should_welcome=True,
        )

    # 已存在用户
    is_first = user.last_active_at is None
    should_welcome = False

    if user.role == "worker":
        # 工人只在首次自动注册时欢迎（上面已处理），已存在工人不再欢迎
        should_welcome = False
    elif user.role in ("factory", "broker"):
        # 厂家/中介首轮欢迎以 last_active_at IS NULL 为准
        should_welcome = is_first

    return UserContext(
        external_userid=external_userid,
        role=user.role,
        status=user.status,
        display_name=user.display_name,
        company=user.company,
        contact_person=user.contact_person,
        phone=user.phone,
        can_search_jobs=bool(user.can_search_jobs),
        can_search_workers=bool(user.can_search_workers),
        is_first_touch=is_first,
        should_welcome=should_welcome,
    )


def check_user_status(user_ctx: UserContext) -> str | None:
    """检查用户状态，返回拦截提示文本或 None（允许继续）。"""
    if user_ctx.status == "blocked":
        return "您的账号已被限制使用，如有疑问请联系客服。"
    if user_ctx.status == "deleted":
        return "账号已进入删除状态，请联系客服处理。"
    return None


def update_last_active(external_userid: str, db: Session) -> None:
    """更新用户活跃时间。"""
    db.query(User).filter(
        User.external_userid == external_userid,
    ).update({"last_active_at": datetime.now(timezone.utc)})


def get_user_status(external_userid: str, db: Session) -> dict:
    """/我的状态：返回账号状态和最近一次提交状态。"""
    user = db.query(User).filter(
        User.external_userid == external_userid,
    ).first()
    if user is None:
        return {"found": False, "message": "未找到您的账号记录。"}

    result = {
        "found": True,
        "role": user.role,
        "status": user.status,
        "registered_at": str(user.registered_at) if user.registered_at else None,
    }

    # 最近一次岗位提交
    latest_job = db.query(Job).filter(
        Job.owner_userid == external_userid,
        Job.deleted_at.is_(None),
    ).order_by(Job.created_at.desc()).first()
    if latest_job:
        result["latest_job"] = {
            "id": latest_job.id,
            "audit_status": latest_job.audit_status,
            "created_at": str(latest_job.created_at),
        }

    # 最近一次简历提交
    latest_resume = db.query(Resume).filter(
        Resume.owner_userid == external_userid,
        Resume.deleted_at.is_(None),
    ).order_by(Resume.created_at.desc()).first()
    if latest_resume:
        result["latest_resume"] = {
            "id": latest_resume.id,
            "audit_status": latest_resume.audit_status,
            "created_at": str(latest_resume.created_at),
        }

    return result


def delete_user_data(external_userid: str, db: Session) -> str:
    """/删除我的信息 编排入口。

    1. 清空 Redis session
    2. 软删除简历
    3. 软删除对话日志（通过 expires_at 设置为当前时间）
    4. 标记 user.status = deleted
    5. 写 conversation_log
    6. 写 audit_log

    返回回复文本。
    """
    now = datetime.now(timezone.utc)

    # 1. 清空 Redis session
    conversation_service.clear_session(external_userid)

    # 2. 软删除简历
    db.query(Resume).filter(
        Resume.owner_userid == external_userid,
        Resume.deleted_at.is_(None),
    ).update({"deleted_at": now})

    # 3. 设置对话日志过期（等价于软删除）
    db.query(ConversationLog).filter(
        ConversationLog.userid == external_userid,
    ).update({"expires_at": now})

    # 4. 标记用户状态
    db.query(User).filter(
        User.external_userid == external_userid,
    ).update({"status": "deleted"})

    # 5. 写 conversation_log
    delete_log = ConversationLog(
        userid=external_userid,
        direction="out",
        msg_type="system",
        content="用户执行了删除操作，数据已进入删除流程。",
        intent="command",
        criteria_snapshot={"command": "delete_my_data"},
        expires_at=now,
    )
    db.add(delete_log)

    # 6. 写 audit_log
    audit_entry = AuditLog(
        target_type="user",
        target_id=external_userid,
        action="auto_pass",  # 用户主动删除，自动通过
        reason="用户主动执行 /删除我的信息",
        operator="system",
    )
    db.add(audit_entry)

    db.flush()

    logger.info("user_service: deleted user data for %s", external_userid)
    return "已收到删除请求，您的资料已进入删除流程。如需恢复，请联系客服。"
