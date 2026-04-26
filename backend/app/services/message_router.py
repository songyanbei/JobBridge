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

import dataclasses
import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.llm.base import IntentResult
from app.llm.prompts import PROMPT_VERSION
from app.models import Resume
from app.schemas.conversation import ReplyMessage, SessionState
from app.services import (
    command_service,
    conversation_service,
    intent_service,
    search_service,
    upload_service,
    user_service,
)
from app.services.intent_service import classify_intent
from app.services.user_service import UserContext
from app.tasks.common import log_event
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

# Stage A：上传草稿相关固定文案（详见 docs/multi-turn-upload-stage-a-implementation.md §3.4）
PENDING_CANCELLED_REPLY = "已取消，岗位草稿已丢弃。"
PENDING_EXPIRED_REPLY = "上次岗位草稿已超时，请整段重新发送岗位信息。"
PENDING_MAX_ROUNDS_REPLY = "信息仍不完整，请整段重新发送岗位信息。"
PENDING_NO_FIELD_REPLY_FMT = "请告诉我具体的{field_name}。"

# Stage C1：upload_conflict 相关文案（spec §2.7 / §9.6）。
CONFLICT_PROMPT_FMT = (
    "当前{kind}还缺“{field_name}”。\n"
    "您要继续发布{kind}，还是先{new_kind}，或取消草稿？\n"
    "回复：继续发布 / 先{new_kind} / 取消草稿"
)
CONFLICT_REPROMPT_FMT = (
    "请明确选择：\n"
    "  · 回复“继续发布”补完{kind}（缺{field_name}）\n"
    "  · 回复“先{new_kind}”丢弃草稿并执行新请求\n"
    "  · 回复“取消草稿”放弃"
)
CONFLICT_DEAD_LOOP_REPLY = "未识别您的选择，已为您丢弃草稿。如需继续操作请整段重新发送。"
CONFLICT_RESUME_FMT = "好的，继续。请告诉我具体的{field_name}。"
CONFLICT_PROCEED_ACK = "草稿已丢弃，正在为您处理新请求。"

# Stage A：cancel 强规则（§9.3 / §3.4）。
# 完整句匹配 → 直接判 cancel；句首匹配 → 判 cancel。
_CANCEL_FULL = {"取消", "不发了", "算了", "先不发了", "不要了"}
_CANCEL_PREFIX = ("不发", "先不", "算了，", "算了,")

# Stage A：判断当前消息是否像“字段补丁”。用于 timeout 后兜底文案。
_PATCH_RE_HEADCOUNT = re.compile(
    r"(?:招\s*)?(?:[一二两三四五六七八九十百千万0-9]+)\s*(?:个人|个|人|位|名)"
)
_PATCH_RE_DIGIT = re.compile(r"^\s*\d{1,5}\s*$")
_PATCH_RE_SALARY = re.compile(r"(?:月薪|薪资|时薪|计件|底薪|\d{4,5}\s*[元块]?|\d+\s*千)")
_KNOWN_SHORT_PATCH_KEYWORDS = (
    "厨师", "保洁", "普工", "保安", "服务员", "电子厂", "服装厂",
    "食品厂", "物流", "仓储", "餐饮", "技工",
)
# 简短城市片段：常见招聘城市（不穷举，命中即可）。
_KNOWN_CITIES = (
    "北京", "上海", "广州", "深圳", "苏州", "昆山", "无锡", "南京", "杭州",
    "宁波", "合肥", "重庆", "成都", "天津", "武汉", "西安", "郑州", "青岛",
    "济南", "厦门", "福州", "长沙",
)

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
    # Stage C1：兜底推导 + self-heal，覆盖测试或非 Redis 路径绕过 load_session 的场景
    conversation_service.ensure_active_flow(session)

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
    # Stage C1（spec §2.11）：构造 session_hint 占位下发；provider 暂不消费。
    session_hint = intent_service.build_session_hint(session)
    try:
        intent_result = classify_intent(
            text=content,
            role=user_ctx.role,
            history=session.history,
            current_criteria=session.search_criteria,
            user_msg_id=msg.msg_id,
            session_hint=session_hint,
        )
    except Exception as exc:
        logger.exception("message_router: classify_intent failed: %s", exc)
        conversation_service.save_session(userid, session)
        return [_reply(userid, SYSTEM_BUSY_REPLY)]

    intent = intent_result.intent

    # Stage C1（spec §2.5）：last_intent 仅观测；current_intent 在 upload_collecting
    # 期间钉在 pending_upload_intent，兼容旧 attach_image 的回落判断。
    session.last_intent = intent
    if (
        session.active_flow == "upload_collecting"
        and session.pending_upload_intent
    ):
        session.current_intent = session.pending_upload_intent
    else:
        session.current_intent = intent

    # Stage C1：active_flow 主路由 + 状态相关命令 guard。
    if intent == "command":
        replies = _route_command_with_state_guard(
            intent_result, msg, user_ctx, session, db,
        )
    elif session.active_flow == "upload_collecting":
        replies = _route_upload_collecting(
            intent_result, msg, user_ctx, session, db,
        )
    elif session.active_flow == "upload_conflict":
        replies = _route_upload_conflict(
            intent_result, msg, user_ctx, session, db,
        )
    elif session.active_flow == "search_active":
        replies = _route_search_active(
            intent_result, msg, user_ctx, session, db,
        )
    else:
        replies = _route_idle(intent_result, msg, user_ctx, session, db)

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
# Stage C1：active_flow 主路由（spec §2.5）
# ---------------------------------------------------------------------------

def _route_idle(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """idle 状态：复用现有 _dispatch_intent 即可；upload/search handler 内部
    会按需把 active_flow 推进到 upload_collecting / search_active。"""
    return _dispatch_intent(intent_result, msg, user_ctx, session, db)


def _route_search_active(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """search_active 状态（spec §2.8）。

    - 新上传意图：清快照/shown，但保留 search_criteria 和 last_criteria，进入上传流程
    - chitchat：保留 search state，回闲聊
    - 其余（follow_up / show_more / search_* / command）：交给 _dispatch_intent
    """
    intent = intent_result.intent
    userid = msg.from_user

    if intent in ("upload_job", "upload_resume", "upload_and_search"):
        session.candidate_snapshot = None
        session.shown_items = []
        # active_flow 暂回 idle，由 upload handler 内部按 missing/success 决定下一步状态
        session.active_flow = "idle"
        return _dispatch_intent(intent_result, msg, user_ctx, session, db)

    if intent == "chitchat":
        return [_reply(userid, _chitchat_text(user_ctx))]

    return _dispatch_intent(intent_result, msg, user_ctx, session, db)


def _route_command_with_state_guard(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """command 路由 + 状态机边界 guard（spec §2.5）。

    全局型命令直接交给 command_service；状态相关命令在 upload_collecting 中需要
    走状态机分支：
      - broker /找岗位 /找工人 in upload_collecting → upload_conflict
      - 其余命令（含 /取消 /重新找）由 command_service 内部处理（已带状态文案）
    """
    cmd = (intent_result.structured_data or {}).get("command", "")

    # broker 在 upload_collecting 中切方向 → 进入 upload_conflict（spec §2.9）
    if (
        session.active_flow == "upload_collecting"
        and user_ctx.role == "broker"
        and cmd in ("switch_to_job", "switch_to_worker")
    ):
        new_intent = "search_job" if cmd == "switch_to_job" else "search_worker"
        synthesized = IntentResult(
            intent=new_intent, structured_data={}, confidence=1.0,
        )
        return _enter_upload_conflict(synthesized, msg, session)

    return _handle_command_intent(intent_result, user_ctx, session, db)


def _route_upload_collecting(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """upload_collecting 状态（spec §2.6 / §9.1-9.5）。

    顺序：
      1. timeout
      2. cancel
      3. chitchat（保留 pending，不递增 failed_patch_rounds）
      4. new business intent → upload_conflict
      5. field patch（含 failed_patch_rounds 计数和退出）
    """
    content = msg.content or ""
    userid = msg.from_user

    # 1. 过期
    if upload_service.is_pending_upload_expired(session):
        was_patch = _looks_like_upload_patch(content)
        upload_service.clear_pending_upload(session)
        if was_patch:
            return [_reply(userid, PENDING_EXPIRED_REPLY)]
        # 未补丁就放行到 idle 分发
        return _route_idle(intent_result, msg, user_ctx, session, db)

    # 2. cancel 强规则
    if _is_cancel(content, intent_result):
        upload_service.clear_pending_upload(session)
        return [_reply(userid, PENDING_CANCELLED_REPLY)]

    # 3. 闲聊穿插（spec §9.8）
    if intent_result.intent == "chitchat":
        awaiting = session.awaiting_field
        field_name = _field_display_name(awaiting) if awaiting else "需要的字段"
        text = (
            f"{_chitchat_text(user_ctx)}\n\n"
            f"您当前还在发布岗位/简历，请补充{field_name}，或发送 /取消 放弃草稿。"
        )
        return [_reply(userid, text)]

    # 4. 新业务意图 → upload_conflict
    if _is_new_business_intent(intent_result, session):
        return _enter_upload_conflict(intent_result, msg, session)

    # 5. 字段补全
    return _handle_field_patch(intent_result, msg, user_ctx, session, db)


def _route_upload_conflict(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """upload_conflict 状态（spec §2.7）。

    用户回复识别（强规则；不再调用 LLM 重新分类）：
      - 取消草稿 → 清 pending，回 idle
      - 继续发布 → 回 upload_collecting
      - 先找/找工人/找岗位 或 LLM intent ∈ search_* → 执行 pending_interruption
      - 其他 → 重复确认；累计 1 次仍未识别则丢弃草稿，避免死循环
    """
    content = (msg.content or "").strip()
    userid = msg.from_user
    intent = intent_result.intent

    # Stage C1（spec §2.7）：识别用户三选一回复时，proceed 信号优先级最高 ——
    # "继续看看" / "算了，先找工人" 这类同时含 resume/cancel 词与 proceed 词的句子，
    # 都按 "用户表达了搜索方向" 处理；这样:
    #   - 裸 "继续" / "继续发布" 仍能正确回 upload_collecting（spec 明文要求）
    #   - "继续看看" 不会被裸 "继续" 抢去 resume
    #   - "算了，先找" 不会被 cancel 强规则吞掉搜索意图
    interruption = session.pending_interruption or {}
    proceed_keywords = ("先找", "找工人", "找岗位", "看简历", "看岗位", "看看")
    has_proceed_signal = (
        any(p in content for p in proceed_keywords)
        or intent in ("search_job", "search_worker")
    )

    # 取消草稿（强规则 cancel 或显式"取消草稿"）—— 仅在不含 proceed 信号时
    if (
        not has_proceed_signal
        and (_is_cancel(content, intent_result) or "取消草稿" in content)
    ):
        upload_service.clear_pending_upload(session)
        return [_reply(userid, PENDING_CANCELLED_REPLY)]

    # 继续发布 —— 仅在不含 proceed 信号时；spec §2.7 要求允许裸 "继续"
    resume_keywords = ("继续发布", "继续填", "继续", "接着发", "接着")
    if not has_proceed_signal and any(p in content for p in resume_keywords):
        session.pending_interruption = None
        session.conflict_followup_rounds = 0
        session.active_flow = "upload_collecting"
        awaiting = session.awaiting_field
        field_name = _field_display_name(awaiting) if awaiting else "需要的字段"
        return [_reply(userid, CONFLICT_RESUME_FMT.format(field_name=field_name))]

    # 执行 pending_interruption（proceed 路径）
    if has_proceed_signal:
        # 用 pending_interruption 复原 IntentResult，避免重新调 LLM
        new_intent_name = (
            interruption.get("intent")
            or (intent if intent in ("search_job", "search_worker", "upload_job", "upload_resume", "upload_and_search") else "search_job")
        )
        new_intent_result = IntentResult(
            intent=new_intent_name,
            structured_data=dict(interruption.get("structured_data") or {}),
            criteria_patch=list(interruption.get("criteria_patch") or []),
            confidence=1.0,
        )
        forwarded_text = (interruption.get("raw_text") or "").strip() or content
        forwarded_msg = dataclasses.replace(msg, content=forwarded_text)

        # 清掉 pending 草稿和 interruption 后再分发
        upload_service.clear_pending_upload(session)
        forwarded = _route_idle(new_intent_result, forwarded_msg, user_ctx, session, db)
        return [_reply(userid, CONFLICT_PROCEED_ACK)] + forwarded

    # 死循环防护
    session.conflict_followup_rounds += 1
    if session.conflict_followup_rounds >= 2:
        upload_service.clear_pending_upload(session)
        return [_reply(userid, CONFLICT_DEAD_LOOP_REPLY)]

    awaiting = session.awaiting_field
    field_name = _field_display_name(awaiting) if awaiting else "字段"
    new_intent_in_interruption = interruption.get("intent", "")
    new_kind = _new_kind_text(new_intent_in_interruption)
    kind = "简历" if session.pending_upload_intent == "upload_resume" else "岗位"
    return [_reply(
        userid,
        CONFLICT_REPROMPT_FMT.format(
            kind=kind, field_name=field_name, new_kind=new_kind,
        ),
    )]


def _enter_upload_conflict(
    intent_result: IntentResult,
    msg: WeComMessage,
    session: SessionState,
) -> list[ReplyMessage]:
    """从 upload_collecting 进入 upload_conflict（spec §9.6）。

    瘦身保存 pending_interruption；保留 pending_upload，让用户决定后再分发。
    """
    session.active_flow = "upload_conflict"
    session.pending_interruption = {
        "intent": intent_result.intent,
        "structured_data": dict(intent_result.structured_data or {}),
        "criteria_patch": list(intent_result.criteria_patch or []),
        "raw_text": msg.content or "",
    }
    session.conflict_followup_rounds = 0

    awaiting = session.awaiting_field
    field_name = _field_display_name(awaiting) if awaiting else "字段"
    kind = "简历" if session.pending_upload_intent == "upload_resume" else "岗位"
    new_kind = _new_kind_text(intent_result.intent)

    log_event(
        "upload_pending_conflict",
        userid=msg.from_user,
        old_flow="upload_collecting",
        new_intent=intent_result.intent,
    )
    return [_reply(
        msg.from_user,
        CONFLICT_PROMPT_FMT.format(
            kind=kind, field_name=field_name, new_kind=new_kind,
        ),
    )]


def _new_kind_text(new_intent: str) -> str:
    if new_intent == "search_worker":
        return "找工人"
    if new_intent == "search_job":
        return "找岗位"
    if new_intent == "upload_job":
        return "发新岗位"
    if new_intent == "upload_resume":
        return "发新简历"
    if new_intent == "upload_and_search":
        return "发新内容并找匹配"
    return "新流程"


def _is_new_business_intent(
    intent_result: IntentResult, session: SessionState,
) -> bool:
    """判定 LLM 抽到的意图是否构成“切到新业务流程”（spec §9.6）。

    1. search_job / search_worker → 必判 True
    2. upload_* 且与 pending_upload_intent 不同 → True
    3. 其余按 field patch 处理（同 origin_intent 即使覆盖既有字段也是 patch）
    """
    intent = intent_result.intent
    if intent in ("search_job", "search_worker"):
        return True
    if intent in ("upload_job", "upload_resume", "upload_and_search"):
        if intent != session.pending_upload_intent:
            return True
    return False


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
    # Stage C1：upload_service 已自行维护 active_flow（保存草稿→upload_collecting；
    # 清空草稿→idle）。这里仅兜底确保 active_flow 与 pending 状态一致。
    if session.pending_upload_intent:
        session.active_flow = "upload_collecting"
    else:
        session.active_flow = "idle"
    return [_reply(msg.from_user, result.reply_text)]


def _handle_upload_and_search(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """上传后顺带检索一次。仅在上传成功时才接着检索。

    Stage C1（spec §9.2.1）：
    - 入库成功后必跑搜索；不论 0 命中还是有结果都写 last_criteria。
    - 有结果 → active_flow=search_active；0 命中 → active_flow=idle，保留 last_criteria。
    """
    # 入库前用 structured_data 构造对侧搜索 criteria，避免 process_upload 清空 pending 后丢字段
    upload_structured = dict(intent_result.structured_data or {})

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
        # 追问（pending 已设）/ 审核拒绝 / 字段缺失 → 不继续检索；
        # active_flow 由 upload_service 内部维护：缺字段 → upload_collecting；max rounds → idle
        if session.pending_upload_intent:
            session.active_flow = "upload_collecting"
        else:
            session.active_flow = "idle"
        return replies

    # 入库成功：active_flow 由 _run_search 根据搜索结果再修正
    direction = _resolve_search_direction(None, user_ctx, session)
    criteria = _build_upload_and_search_criteria(upload_structured, direction)
    if criteria:
        session.search_criteria = {**session.search_criteria, **criteria}

    search_result = _run_search(
        None, criteria, msg.content or "", user_ctx, session, db,
        user_msg_id=msg.msg_id,
    )
    if search_result is None:
        # 搜索 handler 抛错；保持入库成功语义，active_flow 已在 _run_search 回到 idle
        return replies

    # spec §9.2.1：0 命中也要追加“暂未找到”，并保持 active_flow=idle；
    # 有结果时 _run_search 已将 active_flow 推进到 search_active。
    if search_result.reply_text:
        replies.append(_reply(msg.from_user, search_result.reply_text))
    log_event(
        "upload_completed_with_search",
        userid=msg.from_user,
        entity_id=upload_result.entity_id,
        search_result_count=search_result.result_count,
    )
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

    # Stage B P1-1：不能在默认合并前用 session.search_criteria 是否为空短路；
    # 否则 worker "看看新岗位" 这类空 structured_data 场景永远进不到
    # _apply_default_criteria，简历 expected_* 默认条件无机会兜底。
    criteria = dict(session.search_criteria)
    search_result = _run_search(
        intent_result.intent, criteria, msg.content or "", user_ctx, session, db,
        user_msg_id=msg.msg_id,
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

    # Stage B P1-1：同 _handle_search，不在默认合并前因 search_criteria 为空短路。
    # _run_search 会跑 _apply_default_criteria（含 worker 简历兜底），再交给
    # search_service.has_effective_search_criteria 决定是否真正查询。
    # 重新做一次检索：
    # - digest 变化：search_service 会按新 criteria 生成新快照
    # - digest 未变：相当于"再搜一次"，快照会被同样 digest 重置，对用户无感
    # - follow_up 没有显式方向，沿用 session.broker_direction（首次 search 时已写）
    criteria = dict(session.search_criteria)
    search_result = _run_search(
        None, criteria, msg.content or "", user_ctx, session, db,
        user_msg_id=msg.msg_id,
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
    # Stage C1：show_more 后若快照仍存活则保持 search_active；否则降为 idle
    if session.candidate_snapshot is not None:
        session.active_flow = "search_active"
    else:
        session.active_flow = "idle"
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

    # 尝试挂载到当前上传流程。
    # Stage C1（spec §2.10）：优先看 pending_upload_intent — 草稿存活时（含
    # upload_collecting 与 upload_conflict 两态）都应该挂图，避免 current_intent 在
    # 上传过程中被 chitchat / command 等中间消息污染后图片被误判为"非上传流程"。
    # 回落 current_intent 兼容旧 session（C2 删除回落）。
    session = conversation_service.load_session(userid)
    if session and (
        session.pending_upload_intent
        or session.current_intent in ("upload_job", "upload_resume", "upload_and_search")
    ):
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
# Stage A：上传草稿守卫
# ---------------------------------------------------------------------------

def _has_pending_upload(session: SessionState) -> bool:
    """是否存在尚未完成的上传草稿。"""
    return bool(session.pending_upload_intent)


def _is_cancel(content: str, intent_result: IntentResult) -> bool:
    """阶段 A：仅做强规则匹配；不做任意子串匹配。"""
    text = (content or "").strip()
    if not text:
        return False
    if text in _CANCEL_FULL:
        return True
    return text.startswith(_CANCEL_PREFIX)


def _looks_like_upload_patch(content: str) -> bool:
    """当前文本是否像“补字段”表达：人数、薪资、城市/工种片段、纯数字。"""
    if not content:
        return False
    text = content.strip()
    if not text:
        return False
    if _PATCH_RE_DIGIT.match(text):
        return True
    if _PATCH_RE_HEADCOUNT.search(text):
        return True
    if _PATCH_RE_SALARY.search(text):
        return True
    if any(c in text for c in _KNOWN_CITIES):
        return True
    if any(k in text for k in _KNOWN_SHORT_PATCH_KEYWORDS):
        return True
    return False


def _parse_headcount_from_text(text: str) -> int | None:
    """从"2 个人 / 招2人 / 两个"之类文本解析 headcount。

    解析顺序：
      1. 带"个人/个/人/位/名"单位的数字：1-9999 都接受。
      2. 中文小数字（一/两/二…十）：直接映射。
      3. 裸阿拉伯数字（无单位）：限制 1-3 位且 ≤ 999，避免把"7500"之类的薪资数字
         误判为人数（招聘人数实务上 1000 已经是大厂量级）。
    """
    if not text:
        return None
    cn_digits = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    # 1. 带单位匹配（必有单位）
    m_unit = re.search(r"(?:招\s*)?(\d{1,4})\s*(?:个人|个|人|位|名)", text)
    if m_unit:
        try:
            v = int(m_unit.group(1))
            if 0 < v <= 9999:
                return v
        except ValueError:
            pass
    # 2. 中文小数字
    for ch, v in cn_digits.items():
        if ch in text:
            return v
    # 3. 裸数字（无单位）：仅当文本剥掉空格后是纯 1-3 位数字
    m_short = re.fullmatch(r"\s*(\d{1,3})\s*", text)
    if m_short:
        try:
            v = int(m_short.group(1))
            if 0 < v <= 999:
                return v
        except ValueError:
            pass
    return None


def _parse_salary_floor_from_text(text: str) -> int | None:
    """简单解析薪资下限：'7500' / '7500元' / '8千'。"""
    if not text:
        return None
    m = re.search(r"(\d{4,6})", text)
    if m:
        try:
            v = int(m.group(1))
            if 1000 <= v <= 200000:
                return v
        except ValueError:
            pass
    m = re.search(r"(\d{1,3})\s*千", text)
    if m:
        try:
            return int(m.group(1)) * 1000
        except ValueError:
            pass
    return None


def _extract_field_value(
    field: str,
    intent_result: IntentResult,
    raw_text: str,
):
    """按优先级从三个来源抽取某字段的值（structured_data → criteria_patch → 规则）。"""
    # 1. structured_data
    sd = intent_result.structured_data or {}
    val = sd.get(field)
    if not _is_empty(val):
        return val

    # 2. criteria_patch
    for patch in intent_result.criteria_patch or []:
        if patch.get("field") == field:
            v = patch.get("value")
            if not _is_empty(v):
                return v

    # 3. 规则解析（仅覆盖典型上传必填）
    if field == "headcount":
        return _parse_headcount_from_text(raw_text)
    if field == "salary_floor_monthly":
        return _parse_salary_floor_from_text(raw_text)
    if field == "pay_type":
        if "时薪" in raw_text:
            return "时薪"
        if "计件" in raw_text:
            return "计件"
        if "月薪" in raw_text or "底薪" in raw_text:
            return "月薪"
    return None


def _is_empty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, (list, str)) and len(v) == 0:
        return True
    return False


def _merge_other_upload_fields(
    session: SessionState,
    intent_result: IntentResult,
) -> bool:
    """把 structured_data / criteria_patch 中除 awaiting_field 外的有效字段合入 pending。

    返回是否合入了任何新字段。这部分字段补全不视为“答非所问”。
    """
    merged_any = False
    sd = intent_result.structured_data or {}
    pending = dict(session.pending_upload or {})
    for k, v in sd.items():
        if _is_empty(v):
            continue
        if pending.get(k) != v:
            pending[k] = v
            merged_any = True
    for patch in intent_result.criteria_patch or []:
        f = patch.get("field")
        v = patch.get("value")
        if not f or _is_empty(v):
            continue
        if pending.get(f) != v:
            pending[f] = v
            merged_any = True
    if merged_any:
        session.pending_upload = pending
    return merged_any


def _handle_field_patch(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """upload_collecting 字段补全分支（spec §9.2 / §9.5）。

    Stage C1：以 ``failed_patch_rounds`` 作为 max rounds 主退出依据。
    抽取顺序：structured_data → criteria_patch → 正则。
    递增 failed_patch_rounds 的条件：
      1. 三层都没拿到 awaiting_field 的有效值，且没有补到其他有效上传字段。
      2. （理论上）抽到值但范围非法 — 实际由 intent_service 规整层提前丢弃。
    """
    userid = msg.from_user
    raw_text = msg.content or ""
    awaiting = session.awaiting_field

    awaiting_value = None
    if awaiting:
        awaiting_value = _extract_field_value(awaiting, intent_result, raw_text)

    if awaiting and not _is_empty(awaiting_value):
        # 补到了 awaiting_field：merge 主字段，重置 failed_patch_rounds
        pending = dict(session.pending_upload or {})
        pending[awaiting] = awaiting_value
        session.pending_upload = pending
        _merge_other_upload_fields(session, intent_result)
        session.failed_patch_rounds = 0
        return _commit_pending_or_followup(msg, user_ctx, session, db)

    # 未补 awaiting，但合入了其它有效字段 → 不算失败补字段
    other_merged = _merge_other_upload_fields(session, intent_result)
    if other_merged:
        session.failed_patch_rounds = 0
        return _commit_pending_or_followup(msg, user_ctx, session, db)

    # 真正的“答非所问”：累计 failed_patch_rounds，>=2 退出
    session.failed_patch_rounds += 1
    if session.failed_patch_rounds >= 2:
        upload_service.clear_pending_upload(session)
        return [_reply(userid, PENDING_MAX_ROUNDS_REPLY)]

    # 同时维护旧的 follow_up_rounds 作为兼容计数器（spec §2.6 “保留”）
    conversation_service.increment_follow_up(session)
    field_name = _field_display_name(awaiting) if awaiting else "需要的字段"
    return [_reply(userid, PENDING_NO_FIELD_REPLY_FMT.format(field_name=field_name))]


def _commit_pending_or_followup(
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """把当前合入的 pending 草稿喂给 upload_service。

    传入 process_upload 的 raw_text 是“当前轮”用户原文；upload_service 内部
    会将它去重追加到 pending_raw_text_parts，并在入库时拼接所有轮原文。
    后续是否仍缺字段 / 是否入库 / 是否 max rounds 退出，全部由 upload_service 决定。
    """
    userid = msg.from_user
    pending_intent = session.pending_upload_intent or "upload_job"
    pending_data = dict(session.pending_upload or {})
    current_raw = msg.content or ""

    intent_result = IntentResult(
        intent=pending_intent,
        structured_data=pending_data,
        confidence=1.0,
    )

    if pending_intent == "upload_and_search":
        return _handle_upload_and_search(intent_result, msg, user_ctx, session, db)

    result = upload_service.process_upload(
        user_ctx=user_ctx,
        intent_result=intent_result,
        raw_text=current_raw,
        image_keys=[],
        session=session,
        db=db,
    )
    return [_reply(userid, result.reply_text)]


def _field_display_name(field: str) -> str:
    """字段中文展示名（与 upload_service 同步）。"""
    from app.services.upload_service import _FIELD_DISPLAY_NAMES
    return _FIELD_DISPLAY_NAMES.get(field, field)


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
    user_msg_id: str | None = None,
):
    """按 intent + 角色 + session.broker_direction 选择 search_jobs 或 search_workers。

    intent 可以是 search_job / search_worker / upload_and_search / None；
    其中 follow_up / show_more / upload_and_search 不显式指定方向，
    走 session.broker_direction 或角色兜底。

    Stage B：在分发给 search_service 前，按 §3.3 合并默认 criteria：
      1. 当前请求 criteria（已含 session.search_criteria 的累积）
      2. 仅 worker 角色：用户最近一份 passed resume 的 expected_cities /
         expected_job_categories
    已有有效值不会被下层 default 覆盖。

    Phase 7：user_msg_id 透传到 rerank 日志（``llm_call``），便于按消息串联检索链路。
    """
    direction = _resolve_search_direction(intent, user_ctx, session)
    composed = _apply_default_criteria(criteria, session, user_ctx, db, direction)
    if direction == "search_job":
        result = search_service.search_jobs(
            composed, raw_query, session, user_ctx, db, user_msg_id=user_msg_id,
        )
    else:
        result = search_service.search_workers(
            composed, raw_query, session, user_ctx, db, user_msg_id=user_msg_id,
        )

    # Stage C1（spec §2.8 / §9.2.1）：不论命中与否，只要 criteria 有效就写 last_criteria；
    # 并按是否生成 candidate_snapshot 推进 active_flow。
    if search_service.has_effective_search_criteria(composed):
        session.last_criteria = dict(composed)
    if session.candidate_snapshot is not None:
        session.active_flow = "search_active"
    else:
        session.active_flow = "idle"
    return result


# ---------------------------------------------------------------------------
# Stage B：默认 criteria 合并（§3.3）
# ---------------------------------------------------------------------------

def _is_effective_value(v) -> bool:
    """已有有效值的判定：非 None / 非空字符串 / 非空列表。"""
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    if isinstance(v, list) and len(v) == 0:
        return False
    return True


def _build_upload_and_search_criteria(
    structured_data: dict, direction: str,
) -> dict:
    """从 upload_and_search 的 structured_data 抽出对侧搜索的 criteria。

    spec §9.2.1：
      - factory/broker 发岗位 → search_workers，沿用 city / job_category / 薪资
      - worker 发简历 → search_jobs，把 expected_cities → city、
        expected_job_categories → job_category、salary_expect_floor_monthly →
        salary_floor_monthly
    """
    if not structured_data:
        return {}
    sd = dict(structured_data)
    out: dict = {}

    if direction == "search_job":
        # worker 简历方向 → 搜索岗位
        ec = sd.get("expected_cities") or sd.get("city")
        if _is_effective_value(ec):
            out["city"] = ec if isinstance(ec, list) else [ec]
        ej = sd.get("expected_job_categories") or sd.get("job_category")
        if _is_effective_value(ej):
            out["job_category"] = ej if isinstance(ej, list) else [ej]
        salary = sd.get("salary_expect_floor_monthly") or sd.get("salary_floor_monthly")
        if _is_effective_value(salary):
            out["salary_floor_monthly"] = salary
    else:
        # factory/broker 发岗位 → 搜索工人
        city = sd.get("city")
        if _is_effective_value(city):
            out["city"] = city if isinstance(city, list) else [city]
        jc = sd.get("job_category")
        if _is_effective_value(jc):
            out["job_category"] = jc if isinstance(jc, list) else [jc]
        # 把岗位薪资上限作为简历期望薪资的过滤上限
        ceiling = sd.get("salary_ceiling_monthly") or sd.get("salary_floor_monthly")
        if _is_effective_value(ceiling):
            out["salary_ceiling_monthly"] = ceiling
    return out


def _apply_default_criteria(
    criteria: dict,
    session: SessionState,
    user_ctx: UserContext,
    db: Session,
    direction: str,
) -> dict:
    """按 §3.3 固定顺序合并默认 criteria：当前请求 → session → 简历 default。

    “已有有效值不覆盖”：上层 source 提供且有效（非 None / 非空字符串 / 非空列表）
    时，不被下层 default 覆盖。
    """
    composed: dict = dict(criteria or {})

    # Layer 2：session.search_criteria（由 _handle_search / _handle_follow_up 累积）
    for k, v in (session.search_criteria or {}).items():
        if _is_effective_value(v) and not _is_effective_value(composed.get(k)):
            composed[k] = v

    # Layer 3：worker + search_job 方向，从最近 passed resume 取期望城市/工种兜底
    if user_ctx.role == "worker" and direction == "search_job":
        defaults = _load_worker_resume_defaults(user_ctx.external_userid, db)
        for k, v in defaults.items():
            if _is_effective_value(v) and not _is_effective_value(composed.get(k)):
                composed[k] = v

    return composed


def _load_worker_resume_defaults(external_userid: str, db: Session) -> dict:
    """从用户最近一份 passed 简历抽 city / job_category 默认值。

    防御点：
    1. 任何异常（DB 不可用 / schema 漂移）记 warning 并返回空 dict，不挡搜索流程。
    2. 只取最新一份简历，避免历史多份带来的歧义。
    """
    try:
        now = datetime.now(timezone.utc)
        resume = db.query(Resume).filter(
            Resume.owner_userid == external_userid,
            Resume.audit_status == "passed",
            Resume.deleted_at.is_(None),
            Resume.expires_at > now,
        ).order_by(Resume.created_at.desc()).first()
    except Exception:
        logger.exception(
            "message_router: load worker resume defaults failed userid=%s",
            external_userid,
        )
        return {}
    if resume is None:
        return {}
    out: dict = {}
    if resume.expected_cities:
        out["city"] = list(resume.expected_cities)
    if resume.expected_job_categories:
        out["job_category"] = list(resume.expected_job_categories)
    if out:
        log_event(
            "search_default_criteria_applied",
            userid=external_userid,
            source="worker_latest_resume",
            applied_keys=list(out.keys()),
        )
    return out


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
