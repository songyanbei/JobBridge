"""用户服务（Phase 3）。

用户识别、自动注册、状态拦截、欢迎判定、删除编排、/我的状态。
"""
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.exceptions import UserBlocked
from app.core.logging_setup import identifier_hash
from app.core.time_utils import utc_now
from app.models import (
    AuditLog,
    ConversationLog,
    Job,
    Resume,
    User,
)
from app.services import conversation_service, recommendation_privacy_service

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
        logger.info(
            "user_service: auto-registered worker user_hash=%s",
            identifier_hash(external_userid),
        )
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
    ).update({"last_active_at": utc_now()})


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

    # 最近简历按生命周期分组。使用流式 keyset-friendly 排序读取，找到三类
    # 首条后立即停止；不会因固定窗口把较旧但仍在线的简历遮住，也不把全量
    # 历史一次性载入内存。
    from app.config import settings
    from app.services.resume_mutation_service import (
        resume_is_online,
        to_utc_naive,
        utc_now_naive,
    )

    now = utc_now_naive()
    resumes = (
        db.query(Resume)
        .filter(Resume.owner_userid == external_userid)
        .order_by(Resume.created_at.desc(), Resume.id.desc())
        .yield_per(100)
    )
    grouped: dict[str, dict] = {}
    latest_resume = None
    for resume in resumes:
        if latest_resume is None:
            latest_resume = resume
        if resume_is_online(
            resume,
            now=now,
            strict=settings.resume_lifecycle_v2_enabled,
        ):
            lifecycle = "online"
        elif (
            resume.deleted_at is None
            and resume.delist_reason is None
            and resume.activated_at is None
            and resume.expires_at is None
            and resume.candidate_expires_at is not None
            and to_utc_naive(resume.candidate_expires_at) > now
        ):
            lifecycle = "candidate"
        else:
            lifecycle = "history"
        grouped.setdefault(lifecycle, {
            "id": resume.id,
            "audit_status": resume.audit_status,
            "lifecycle_status": lifecycle,
            "created_at": str(resume.created_at),
        })
        if len(grouped) == 3:
            break

    for lifecycle, item in grouped.items():
        result[f"latest_{lifecycle}_resume"] = item
    if latest_resume is not None:
        result["latest_resume"] = {
            "id": latest_resume.id,
            "audit_status": latest_resume.audit_status,
            "lifecycle_status": next(
                (
                    name for name, item in grouped.items()
                    if item["id"] == latest_resume.id
                ),
                "history",
            ),
            "created_at": str(latest_resume.created_at),
        }

    return result


def delete_user_data(external_userid: str, db: Session) -> str:
    """/删除我的信息 编排入口。

    1. 清空 Redis session
    2. 软删除简历
    3. 软删除对话日志（通过 expires_at 设置为当前时间）
    4. 立即清空推荐 delivery 正文与 prepared session patch（方案 §9.11.1 行 2147）
    5. 标记 user.status = deleted
    6. 写 conversation_log
    7. 写 audit_log

    这里**只做脱敏，不删事实行**：推荐 request/attempt/delivery/impression/event
    与候选侧的反查清理由延迟硬删任务
    ``app.tasks.recommendation_privacy_cleanup`` 调
    ``recommendation_privacy_service.delete_recommendation_user_data()`` 完成，
    严格按 §9.11 的外键顺序执行。命令阶段不做假名化：把 ``viewer_userid`` 改成
    稳定哈希既违反 §14.12「不得保留稳定哈希」，也会让延迟硬删再也按 userid
    反查不到这些行。

    返回回复文本。
    """
    now = utc_now()

    # 1. 清空 Redis session
    conversation_service.clear_session(external_userid)

    # 2. 逐份锁定并关闭 replacement；状态、版本与 cleanup task 同事务写入。
    from app.services.job_media_service import mark_resume_media_delete_pending
    from app.services.resume_mutation_service import (
        close_active_replacement, increment_resume_version, to_utc_naive,
    )
    from app.services.target_cleanup_service import ensure_target_cleanup_task

    deleted_at = to_utc_naive(now)
    # Candidate creation uses User -> Resume -> relation; deletion follows the
    # same prefix so it cannot invert locks with an in-flight replacement.
    locked_user = db.query(User).filter(
        User.external_userid == external_userid,
    ).with_for_update().one_or_none()
    resume_ids = [
        int(row[0]) for row in db.query(Resume.id).filter(
            Resume.owner_userid == external_userid,
        ).order_by(Resume.id).all()
    ]
    for resume_id in resume_ids:
        resume = close_active_replacement(db, resume_id, reason="user_deleted")
        if resume.deleted_at is None:
            resume.deleted_at = deleted_at
            resume.delist_reason = "user_deleted"
            increment_resume_version(resume)
        mark_resume_media_delete_pending(db, resume_id)
        ensure_target_cleanup_task(db, "resume", resume_id, reason="user_deleted")

    # 3. 设置对话日志过期（等价于软删除）
    db.query(ConversationLog).filter(
        ConversationLog.userid == external_userid,
    ).update({"expires_at": now})

    # 4. 推荐正文与 session patch 立即销毁，不等 TTL、不等延迟硬删（§9.11.1 行 2147、
    #    §14.12 行 3392）。失败不能阻断删除命令本身，转由重试队列兜底。
    try:
        recommendation_privacy_service.redact_user_recommendation_content(
            db, external_userid, now=now,
        )
    except Exception as exc:
        # 只记异常类名：ORM 异常会把 SQL 绑定参数（含 userid）一起带进日志。
        logger.error(
            "user_service: immediate recommendation redaction failed error=%s",
            type(exc).__name__,
        )
        recommendation_privacy_service.enqueue_privacy_retry(
            external_userid,
            batch_id="delete_command",
            failed_steps=["redact_own_content"],
        )

    # 5. 标记用户状态
    if locked_user is not None:
        locked_user.status = "deleted"

    # 6. 写 conversation_log
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

    # 7. 写 audit_log
    audit_entry = AuditLog(
        target_type="user",
        target_id=external_userid,
        action="auto_pass",  # 用户主动删除，自动通过
        reason="用户主动执行 /删除我的信息",
        operator="system",
    )
    db.add(audit_entry)

    # 8. 记录硬删倒计时起点到 User.extra['deleted_at']（Phase 7 §3.1 模块 C）。
    #    存储为 MySQL 友好的 UTC 字符串，供 ttl_cleanup 用 STR_TO_DATE 解析；
    #    不用 isoformat()（会带 "T" 和时区后缀）。
    user = db.query(User).filter(
        User.external_userid == external_userid,
    ).one_or_none()
    if user is not None:
        extra = dict(user.extra or {})
        extra["deleted_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        user.extra = extra

    db.flush()

    logger.info(
        "user_service: deleted user data user_hash=%s",
        identifier_hash(external_userid),
    )
    return "已收到删除请求，您的资料已进入删除流程。如需恢复，请联系客服。"
