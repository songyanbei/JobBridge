"""消息路由编排（Phase 4）。

职责：把 Worker 喂进来的 WeComMessage 变成一组 ReplyMessage，
不负责发送、不负责图片下载、不直接依赖 app.wecom.client。

处理链路：
1. 用户识别（user_service.identify_or_register）
2. 状态拦截（blocked / deleted 短路）
3. 更新 last_active_at
4. 按消息类型分流：
   - text  → _handle_text
   - image → _handle_image（依赖 Worker 已填充的 msg.image_url）
   - voice → 回复不支持
   - 其它（file / video / link / location）→ 回复不支持
   - event → 仅记录日志，返回空列表
5. 文本链路内部：
   - 首次交互直接回欢迎语（优先于意图分类）
   - intent_service.classify_intent 统一识别（显式命令 → show_more → LLM）
   - 按意图分发（命令 / 上传 / 检索 / 追问 / 翻页 / 闲聊）
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.llm.base import IntentResult
from app.llm.prompts import PROMPT_VERSION
from app.schemas.conversation import ReplyMessage, SessionState
from app.services import (
    command_service,
    conversation_service,
    search_service,
    upload_service,
    user_service,
)
from app.services.intent_service import classify_intent
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 固定回复文案
# ---------------------------------------------------------------------------

BLOCKED_REPLY = "您的账号已被限制使用，如有疑问请联系客服。"
DELETED_REPLY = "账号已进入删除状态，请联系客服处理。"
VOICE_NOT_SUPPORTED = "暂不支持语音，请发送文字。"
FILE_NOT_SUPPORTED = "暂不支持文件，请直接用文字描述。"
UNKNOWN_TYPE_REPLY = "暂不支持该消息类型，请发送文字。"
RATE_LIMITED_REPLY = "您发送太频繁了，请稍后再试。"
SYSTEM_BUSY_REPLY = "系统繁忙，请稍后再试。"
FALLBACK_REPLY = (
    "抱歉，我没有理解您的意思。您可以直接告诉我您想找什么工作，或输入 /帮助 查看使用指南。"
)
IMAGE_RECEIVED_NON_UPLOAD = (
    "图片已收到。目前仅支持文字描述发布信息，图片作为附件留存。"
)
IMAGE_DOWNLOAD_FAILED = "图片处理失败，请稍后重试。"

_WELCOME_WORKER = (
    "您好，欢迎使用 JobBridge 招工助手！\n"
    "直接告诉我您的需求，例如：\n"
    "  · 苏州找电子厂，5000以上，包吃住\n"
    "  · 昆山找普工，期望月薪 6000\n"
    "输入 /帮助 查看更多指令。"
)


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

def process(msg: WeComMessage, db: Session) -> list[ReplyMessage]:
    """消息路由主入口。Worker 调用，返回待发送的回复列表。"""
    userid = msg.from_user
    if not userid:
        logger.warning("message_router: empty from_user in msg_id=%s", msg.msg_id)
        return []

    # 1. 用户识别 / 注册
    try:
        user_ctx = user_service.identify_or_register(userid, db)
    except Exception as exc:
        logger.exception("message_router: identify_or_register failed: %s", exc)
        return [_reply(userid, SYSTEM_BUSY_REPLY)]

    # 2. 状态拦截（blocked / deleted 短路）
    block_text = user_service.check_user_status(user_ctx)
    if block_text is not None:
        return [_reply(userid, block_text)]

    # 3. 活跃时间更新（幂等、廉价操作，安全放在最前）
    try:
        user_service.update_last_active(userid, db)
    except Exception:
        logger.exception("message_router: update_last_active failed (non-fatal)")

    # 4. 按消息类型分流
    mtype = msg.msg_type or ""
    if mtype == "text":
        return _handle_text(msg, user_ctx, db)
    if mtype == "image":
        return _handle_image(msg, user_ctx, db)
    if mtype == "voice":
        return [_reply(userid, VOICE_NOT_SUPPORTED)]
    if mtype == "event":
        logger.info("message_router: wecom event received userid=%s content=%s",
                    userid, msg.content)
        return []
    if mtype in ("file", "video", "link", "location"):
        return [_reply(userid, FILE_NOT_SUPPORTED)]
    # 未知类型兜底
    logger.warning("message_router: unknown msg_type=%s from userid=%s", mtype, userid)
    return [_reply(userid, UNKNOWN_TYPE_REPLY)]


# ---------------------------------------------------------------------------
# 文本链路
# ---------------------------------------------------------------------------

def _handle_text(
    msg: WeComMessage,
    user_ctx: UserContext,
    db: Session,
) -> list[ReplyMessage]:
    userid = msg.from_user
    content = (msg.content or "").strip()

    # 空文本兜底（企微理论上不会推空文本）
    if not content:
        return [_reply(userid, FALLBACK_REPLY)]

    # 加载 / 创建 session
    session = conversation_service.load_session(userid)
    if session is None:
        session = conversation_service.create_session(userid, user_ctx.role)

    # 首次欢迎优先于意图分类
    if user_ctx.should_welcome:
        conversation_service.record_history(session, "user", content)
        welcome = _build_welcome(user_ctx)
        conversation_service.record_history(session, "assistant", welcome)
        conversation_service.save_session(userid, session)
        return [_reply(userid, welcome)]

    # 先把当前用户消息写入 history，再让 classify_intent 看到完整上下文
    conversation_service.record_history(session, "user", content)

    # 统一意图分类（命令 / show_more / LLM 三级）
    try:
        intent_result = classify_intent(
            text=content,
            role=user_ctx.role,
            history=session.history,
            current_criteria=session.search_criteria,
        )
    except Exception as exc:
        logger.exception("message_router: classify_intent failed: %s", exc)
        conversation_service.save_session(userid, session)
        return [_reply(userid, SYSTEM_BUSY_REPLY)]

    intent = intent_result.intent
    session.current_intent = intent

    replies = _dispatch_intent(intent_result, msg, user_ctx, session, db)

    # 把出站回复写入 history（只记第一条，避免历史爆炸）
    if replies:
        conversation_service.record_history(session, "assistant", replies[0].content)

    conversation_service.save_session(userid, session)
    return replies


def _dispatch_intent(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """按意图分发到具体 handler。"""
    userid = msg.from_user
    intent = intent_result.intent

    try:
        if intent == "command":
            return _handle_command_intent(intent_result, user_ctx, session, db)
        if intent in ("upload_job", "upload_resume"):
            return _handle_upload(intent_result, msg, user_ctx, session, db)
        if intent == "upload_and_search":
            return _handle_upload_and_search(intent_result, msg, user_ctx, session, db)
        if intent in ("search_job", "search_worker"):
            return _handle_search(intent_result, msg, user_ctx, session, db)
        if intent == "follow_up":
            return _handle_follow_up(intent_result, msg, user_ctx, session, db)
        if intent == "show_more":
            return _handle_show_more(msg, user_ctx, session, db)
        if intent == "chitchat":
            return [_reply(userid, _chitchat_text(user_ctx))]
        # 未知意图兜底
        logger.warning("message_router: unknown intent=%s userid=%s", intent, userid)
        return [_reply(userid, FALLBACK_REPLY)]
    except Exception as exc:
        logger.exception("message_router: dispatch intent=%s failed: %s", intent, exc)
        return [_reply(userid, SYSTEM_BUSY_REPLY)]


# ---------------------------------------------------------------------------
# 各意图 handler
# ---------------------------------------------------------------------------

def _handle_command_intent(
    intent_result: IntentResult,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    data = intent_result.structured_data or {}
    cmd = data.get("command", "")
    args = data.get("args", "") or ""
    return command_service.execute(cmd, args, user_ctx, session, db)


def _handle_upload(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    result = upload_service.process_upload(
        user_ctx=user_ctx,
        intent_result=intent_result,
        raw_text=msg.content or "",
        image_keys=[],  # 图片在 _handle_image 单独处理
        session=session,
        db=db,
    )
    return [_reply(msg.from_user, result.reply_text)]


def _handle_upload_and_search(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """上传后顺带检索一次。仅在上传成功时才接着检索。"""
    upload_result = upload_service.process_upload(
        user_ctx=user_ctx,
        intent_result=intent_result,
        raw_text=msg.content or "",
        image_keys=[],
        session=session,
        db=db,
    )

    replies: list[ReplyMessage] = [_reply(msg.from_user, upload_result.reply_text)]

    if not upload_result.success:
        # 追问 / 审核拒绝 / 字段缺失 → 不继续检索
        return replies

    # 上传成功后，用当前 structured_data 做一次检索
    criteria = dict(intent_result.structured_data or {})
    session.search_criteria = {**session.search_criteria, **criteria}

    # upload_and_search 的方向：
    #   - 工人：search_job（找工作）
    #   - 厂家/中介：search_worker（找工人）
    # 直接让 _resolve_search_direction 按角色兜底即可（传 None）
    search_result = _run_search(
        None, criteria, msg.content or "", user_ctx, session, db,
    )
    if search_result is not None and search_result.reply_text:
        replies.append(_reply(msg.from_user, search_result.reply_text))
    return replies


def _handle_search(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    # 首次搜索：把 LLM 抽到的 structured_data 累积到 session.search_criteria
    # 即使本轮因为缺字段追问返回，也要保留部分条件，下一轮 follow_up 才有据可依
    new_criteria = dict(intent_result.structured_data or {})
    if new_criteria:
        session.search_criteria = {**session.search_criteria, **new_criteria}

    # 必填字段不齐 → 追问
    missing = list(intent_result.missing_fields or [])
    if missing:
        return [_reply(
            msg.from_user,
            _missing_follow_up_text(missing),
            intent=intent_result.intent,
            criteria_snapshot=_snapshot_meta(session),
        )]

    criteria = dict(session.search_criteria)
    if not criteria:
        # 极端兜底：LLM 返回 search 意图但 structured_data 为空且 session 也空
        return [_reply(msg.from_user, FALLBACK_REPLY)]

    search_result = _run_search(
        intent_result.intent, criteria, msg.content or "", user_ctx, session, db,
    )
    if search_result is None:
        return [_reply(msg.from_user, SYSTEM_BUSY_REPLY)]
    return [_reply(
        msg.from_user,
        search_result.reply_text,
        intent=intent_result.intent,
        criteria_snapshot=_snapshot_meta(session),
    )]


def _handle_follow_up(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    # 把 patch 合并进 session.search_criteria；若 digest 变化会自动清快照
    conversation_service.merge_criteria_patch(
        session, intent_result.criteria_patch or [],
    )

    if not session.search_criteria:
        return [_reply(msg.from_user, FALLBACK_REPLY)]

    # 重新做一次检索：
    # - digest 变化：search_service 会按新 criteria 生成新快照
    # - digest 未变：相当于"再搜一次"，快照会被同样 digest 重置，对用户无感
    # - follow_up 没有显式方向，沿用 session.broker_direction（首次 search 时已写）
    criteria = dict(session.search_criteria)
    search_result = _run_search(
        None, criteria, msg.content or "", user_ctx, session, db,
    )
    if search_result is None:
        return [_reply(msg.from_user, SYSTEM_BUSY_REPLY)]
    return [_reply(
        msg.from_user,
        search_result.reply_text,
        intent=intent_result.intent,
        criteria_snapshot=_snapshot_meta(session),
    )]


def _handle_show_more(
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    result = search_service.show_more(session, user_ctx, db)
    return [_reply(
        msg.from_user,
        result.reply_text,
        intent="show_more",
        criteria_snapshot=_snapshot_meta(session),
    )]


# ---------------------------------------------------------------------------
# 图片消息
# ---------------------------------------------------------------------------

def _handle_image(
    msg: WeComMessage,
    user_ctx: UserContext,
    db: Session,
) -> list[ReplyMessage]:
    userid = msg.from_user
    image_url = msg.image_url

    if not image_url:
        logger.warning("message_router: image msg without image_url, msg_id=%s", msg.msg_id)
        return [_reply(userid, IMAGE_DOWNLOAD_FAILED)]

    # 尝试挂载到当前上传流程
    session = conversation_service.load_session(userid)
    if session and session.current_intent in ("upload_job", "upload_resume", "upload_and_search"):
        feedback = upload_service.attach_image(
            external_userid=userid,
            image_key=image_url,
            session=session,
            db=db,
        )
        conversation_service.save_session(userid, session)
        return [_reply(userid, feedback)]

    # 非上传流程：留存提示
    return [_reply(userid, IMAGE_RECEIVED_NON_UPLOAD)]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _run_search(
    intent: str | None,
    criteria: dict,
    raw_query: str,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
):
    """按 intent + 角色 + session.broker_direction 选择 search_jobs 或 search_workers。

    intent 可以是 search_job / search_worker / upload_and_search / None；
    其中 follow_up / show_more / upload_and_search 不显式指定方向，
    走 session.broker_direction 或角色兜底。
    """
    direction = _resolve_search_direction(intent, user_ctx, session)
    if direction == "search_job":
        return search_service.search_jobs(criteria, raw_query, session, user_ctx, db)
    return search_service.search_workers(criteria, raw_query, session, user_ctx, db)


def _resolve_search_direction(
    intent: str | None,
    user_ctx: UserContext,
    session: SessionState,
) -> str:
    """决定当前请求走 search_job 还是 search_worker。

    规则：
    - worker：永远 search_job（只能找岗位）
    - 显式 intent=search_job/search_worker：尊重 intent；broker 场景同步写
      session.broker_direction 以便后续 follow_up / show_more 沿用
    - 否则（follow_up / show_more / upload_and_search）：
      * broker：沿用 session.broker_direction，没有则默认 search_job
      * factory：默认 search_worker
    """
    if user_ctx.role == "worker":
        return "search_job"

    if intent == "search_job":
        if user_ctx.role == "broker":
            session.broker_direction = "search_job"
        return "search_job"
    if intent == "search_worker":
        if user_ctx.role == "broker":
            session.broker_direction = "search_worker"
        return "search_worker"

    # 无显式 intent → 沿用 session / 角色默认
    if user_ctx.role == "broker":
        return session.broker_direction or "search_job"
    # factory
    return "search_worker"


def _missing_follow_up_text(missing: list[str]) -> str:
    from app.services.upload_service import _FIELD_DISPLAY_NAMES  # 局部 import 避免 api 层循环
    names = [_FIELD_DISPLAY_NAMES.get(f, f) for f in missing]
    if len(names) <= 2:
        return f"信息还不够完整，请补充：{'、'.join(names)}。"
    lines = "\n".join(f"- {n}" for n in names)
    return f"信息还不够完整，请补充：\n{lines}"


def _chitchat_text(user_ctx: UserContext) -> str:
    if user_ctx.role == "worker":
        return (
            "您好！可以直接告诉我您想找什么工作，例如：\n"
            "  · 苏州找电子厂，5000 以上，包吃住\n"
            "  · 昆山找普工，期望月薪 6000\n"
            "输入 /帮助 查看更多指令。"
        )
    if user_ctx.role == "factory":
        return (
            "您好！可以直接告诉我您要发布的岗位信息，例如：\n"
            "  · 苏州电子厂招普工 30 人，5500 月薪包吃住\n"
            "输入 /帮助 查看更多指令。"
        )
    if user_ctx.role == "broker":
        return (
            "您好！您可以：\n"
            "  · 发送 /找岗位 切换到找岗位模式\n"
            "  · 发送 /找工人 切换到找工人模式\n"
            "  · 直接描述需求由我自动识别"
        )
    return FALLBACK_REPLY


def _build_welcome(user_ctx: UserContext) -> str:
    if user_ctx.role == "worker":
        return _WELCOME_WORKER
    if user_ctx.role == "factory":
        prefix = ""
        if user_ctx.company and user_ctx.contact_person:
            prefix = f"您好，{user_ctx.company} 的 {user_ctx.contact_person}！\n"
        elif user_ctx.company:
            prefix = f"您好，{user_ctx.company}！\n"
        return (
            f"{prefix}欢迎使用 JobBridge 招工助手。\n"
            "您可以直接描述要发布的岗位信息，例如：\n"
            "  · 苏州电子厂招普工 30 人，5500 月薪包吃住\n"
            "输入 /帮助 查看更多指令。"
        )
    if user_ctx.role == "broker":
        prefix = ""
        if user_ctx.display_name:
            prefix = f"您好，中介 {user_ctx.display_name}！\n"
        return (
            f"{prefix}欢迎使用 JobBridge 招工助手。\n"
            "您可以：\n"
            "  · 发送 /找岗位 切换到找岗位模式\n"
            "  · 发送 /找工人 切换到找工人模式\n"
            "输入 /帮助 查看更多指令。"
        )
    return _WELCOME_WORKER


def _snapshot_meta(session: SessionState) -> dict:
    """给 Worker 写 conversation_log.criteria_snapshot 的附加数据。"""
    return {
        "criteria": dict(session.search_criteria),
        "prompt_version": PROMPT_VERSION,
        "broker_direction": session.broker_direction,
    }


def _reply(
    userid: str,
    content: str,
    intent: str | None = None,
    criteria_snapshot: dict | None = None,
) -> ReplyMessage:
    """构造 ReplyMessage；intent / criteria_snapshot 将被 Worker 落 conversation_log。"""
    return ReplyMessage(
        userid=userid,
        content=content,
        intent=intent,
        criteria_snapshot=criteria_snapshot,
    )
