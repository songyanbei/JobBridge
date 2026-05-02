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

from app.config import settings as _settings_module
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
from app.services.intent_service import (
    _SALARY_MAX,
    _SALARY_MIN,
    _legacy_compute_missing,
    classify_dialogue,
    classify_intent,
)
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

# 阶段二（dialogue-intent-extraction-phased-plan §2.1.4）：clarification 反问模板。
# 不依赖 LLM 文案；按 clarification.kind 渲染稳定文本，便于断言和回归。
_V2_CLAR_CITY_REPLACE_OR_ADD = (
    "您是只看{new_city}，还是{old_city}和{new_city}都看？\n"
    "回复：只看{new_city} / {old_city}和{new_city}都看"
)
_V2_CLAR_CITY_REPLACE_OR_ADD_FALLBACK = (
    "您是只看新城市，还是新旧城市都看？\n"
    "回复：只看新城市 / 新旧都看"
)
_V2_CLAR_LOW_CONFIDENCE = (
    "您的需求我没太确定，方便再描述一下吗？比如想找哪个城市、什么类型的工作。"
)
_V2_CLAR_FRAME_CONFLICT = (
    "您当前还有未完成的草稿，是要继续完成草稿，还是先做新请求？"
)
_V2_CLAR_ROLE_NO_PERMISSION = (
    "当前账号不支持该操作。如需调整，请联系运营或先切换角色。"
)
_V2_CLAR_DEFAULT = "请再说得具体一些，方便我帮您处理。"


def _render_v2_clarification(clarification: dict, session: SessionState) -> str:
    """按 clarification.kind 渲染反问文案。"""
    clar = clarification or {}
    kind = clar.get("kind") or ""
    if kind == "city_replace_or_add":
        # 优先使用 reducer 携带的 new_value / old_value（具体城市名）；
        # 退化到 session.search_criteria.city + 通用文案。
        old_list = clar.get("old_value")
        if not old_list:
            old_list = (session.search_criteria or {}).get("city") or []
        new_list = clar.get("new_value") or []
        if isinstance(old_list, list) and old_list and isinstance(new_list, list) and new_list:
            old_city = "、".join(str(v) for v in old_list)
            new_city = "、".join(str(v) for v in new_list)
            return _V2_CLAR_CITY_REPLACE_OR_ADD.format(
                old_city=old_city, new_city=new_city,
            )
        return _V2_CLAR_CITY_REPLACE_OR_ADD_FALLBACK
    if kind == "low_confidence":
        return _V2_CLAR_LOW_CONFIDENCE
    if kind == "frame_conflict":
        return _V2_CLAR_FRAME_CONFLICT
    if kind == "role_no_permission":
        return _V2_CLAR_ROLE_NO_PERMISSION
    return _V2_CLAR_DEFAULT

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

    # 先把当前用户消息写入 history，再让 LLM 看到完整上下文
    conversation_service.record_history(session, "user", content)

    # 阶段二（dialogue-intent-extraction-phased-plan §2.3）：
    # mode=off 时直接走 legacy classify_intent 路径，保持已有调用点 / 测试兼容；
    # mode=shadow / dual_read 时走 classify_dialogue 入口，里面再决定要不要旁路 v2。
    v2_mode = getattr(_settings_module, "dialogue_v2_mode", "off")
    decision = None  # type: ignore[assignment]
    source = "legacy"

    if v2_mode == "off":
        # session_hint 只在 legacy 路径下需要在此处构造；classify_dialogue 内部会自己构造。
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
    else:
        try:
            route = classify_dialogue(
                text=content,
                role=user_ctx.role,
                history=session.history,
                session=session,
                user_msg_id=msg.msg_id,
                userid=userid,
            )
        except Exception as exc:
            logger.exception("message_router: classify_dialogue failed: %s", exc)
            conversation_service.save_session(userid, session)
            return [_reply(userid, SYSTEM_BUSY_REPLY)]
        intent_result = route.intent_result
        decision = route.decision
        source = route.source

        # 阶段二 v2 分支：dual_read 命中
        if source == "v2_dual_read" and decision is not None:
            from app.services.dialogue_applier import apply_awaiting_ops, apply_decision
            # awaiting_ops 必须在所有 v2 分支上执行（包括 clarification / 冲突短路），
            # 否则被消费的 awaiting 字段会僵尸保留（adversarial review C1/I15）。
            apply_awaiting_ops(decision, session)
            if decision.clarification:
                # 直接渲染反问，不走 _route_*
                reply_text = _render_v2_clarification(decision.clarification, session)
                conversation_service.record_history(session, "assistant", reply_text)
                conversation_service.save_session(userid, session)
                return [_reply(userid, reply_text)]
            # enter_upload_conflict：直接调现成的 _enter_upload_conflict
            # 既写状态又生成 CONFLICT_PROMPT_FMT，避免在 applier 里复制冲突文案逻辑。
            if decision.state_transition == "enter_upload_conflict":
                replies = _enter_upload_conflict(intent_result, msg, session)
                if replies:
                    conversation_service.record_history(
                        session, "assistant", replies[0].content,
                    )
                conversation_service.save_session(userid, session)
                return replies
            # resolve_conflict（dialogue-intent-extraction-phased-plan §2.1.8）：
            # codex review P1 防回归。compat 派生的 IntentResult(intent="command")
            # 缺 structured_data.command，会落到 UNKNOWN_COMMAND；这里直接按
            # state_transition 渲染对应 ack 文案 / 派发 pending_interruption，
            # 不走通用 command 路由。
            if decision.dialogue_act == "resolve_conflict":
                # 关键：先调 applier 物化 state_transition（cancel_draft →
                # clear_pending_upload / resume_pending_upload →
                # resume_upload_collecting / proceed_with_new →
                # apply_pending_interruption），再让 _route_v2_resolve_conflict
                # 渲染对应文案（以及 proceed 路径消费 pending_interruption）。
                # 否则 cancel/resume 只改回复但 session 状态不动，是真 bug
                # （codex review 第二轮 P1）。
                apply_decision(decision, session, msg=msg, intent_result=intent_result)
                replies = _route_v2_resolve_conflict(
                    decision, msg, user_ctx, session, db,
                )
                if replies:
                    conversation_service.record_history(
                        session, "assistant", replies[0].content,
                    )
                conversation_service.save_session(userid, session)
                return replies
            # 其它 transition → applier 物化（awaiting_ops 已经 apply 过，applier 内部
            # 重复调用也是幂等的：consume_search_awaiting 对已消费字段是 no-op）。
            apply_decision(decision, session, msg=msg, intent_result=intent_result)

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


def _route_v2_resolve_conflict(
    decision,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage]:
    """阶段二 v2 dual_read 下处理 resolve_conflict（codex review P1 修复）。

    职责分工（调用前提：调用方已经先跑过 apply_decision）：
    - applier：物化 state_transition 到 session（清/恢复 pending_upload、
      切 active_flow、清 pending_interruption 等）。
    - 本函数：只生成回复文案 + 在 proceed 路径上消费 pending_interruption
      作为新意图派发。**不再重复修改 session 上传/冲突状态字段**。

    与 legacy `_route_upload_conflict` 的区别：legacy 用 keyword 推断用户意图，
    这里直接信任 LLM 输出的 conflict_action（reducer 已映射成 transition）。
    """
    userid = msg.from_user
    transition = decision.state_transition

    # cancel_draft：applier 已经清了 pending_upload + active_flow=idle +
    # pending_interruption=None。这里只渲染回复文案。
    if transition == "clear_pending_upload":
        return [_reply(userid, PENDING_CANCELLED_REPLY)]

    # resume_pending_upload：applier 已经把 active_flow 改回 upload_collecting +
    # pending_interruption=None。这里读 awaiting_field 渲染 CONFLICT_RESUME_FMT。
    if transition == "resume_upload_collecting":
        awaiting = session.awaiting_field
        field_name = _field_display_name(awaiting) if awaiting else "需要的字段"
        return [_reply(userid, CONFLICT_RESUME_FMT.format(field_name=field_name))]

    # proceed_with_new：applier 设了 active_flow=idle 但保留 pending_interruption
    # 给本函数读。读完后清 pending_interruption + clear_pending_upload，再派发新意图。
    if transition == "apply_pending_interruption":
        interruption = dict(session.pending_interruption or {})
        new_intent_name = (
            interruption.get("intent")
            or "search_job"  # 安全 fallback
        )
        new_intent_result = IntentResult(
            intent=new_intent_name,
            structured_data=dict(interruption.get("structured_data") or {}),
            criteria_patch=list(interruption.get("criteria_patch") or []),
            confidence=1.0,
        )
        forwarded_text = (interruption.get("raw_text") or "").strip() or msg.content
        forwarded_msg = dataclasses.replace(msg, content=forwarded_text)

        # 消费 pending_interruption + 清草稿（applier 只清了 active_flow）
        upload_service.clear_pending_upload(session)
        session.pending_interruption = None

        forwarded = _route_idle(new_intent_result, forwarded_msg, user_ctx, session, db)
        return [_reply(userid, CONFLICT_PROCEED_ACK)] + forwarded

    # 兜底（理论上不该走到这里 —— reducer 不会输出其它 transition for resolve_conflict）
    logger.warning(
        "_route_v2_resolve_conflict: unexpected transition=%s, falling back to UNKNOWN",
        transition,
    )
    return [_reply(userid, FALLBACK_REPLY)]


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
    # Phase 1：进入上传流程时清搜索 awaiting，避免上传草稿的裸值（如 headcount 的 "2"）
    # 与搜索 awaiting 的薪资字段（"2500"）混淆。
    conversation_service.clear_search_awaiting(session)
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

    Phase 1：进入此路径同时也清搜索 awaiting，避免与上传 awaiting 互相污染。

    Stage C1（spec §9.2.1）：
    - 入库成功后必跑搜索；不论 0 命中还是有结果都写 last_criteria。
    - 有结果 → active_flow=search_active；0 命中 → active_flow=idle，保留 last_criteria。
    """
    conversation_service.clear_search_awaiting(session)
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

    # Phase 1（dialogue-intent-extraction-phased-plan §1.1.3）：frame 校正后，
    # 用临时 legacy schema 重算 missing，不直接信任 LLM 的 missing_fields。
    # 旧 _compute_search_missing 仍保留作为 fallback：当 frame 不属于搜索 frame
    # 时（极罕见）退回旧逻辑。
    #
    # Stage B P1-1 兼容：worker "看看新岗位" 这类 structured_data 与 LLM missing 都为空、
    # 完全靠简历兜底默认条件的场景，仍按 LLM 走（让 _run_search → _apply_default_criteria
    # 注入 worker 简历的 expected_cities / expected_job_categories）。否则会被 legacy
    # 强制要求 city + job_category 而错失资源。
    frame = _search_frame_for_intent(intent_result.intent)
    if frame:
        llm_missing = list(intent_result.missing_fields or [])
        relies_on_defaults = (not new_criteria) and (not llm_missing)
        if relies_on_defaults:
            missing = _compute_search_missing(intent_result, session)
        else:
            missing = _legacy_compute_missing(frame, session.search_criteria)
            # 过滤掉 candidate_search 的"city|job_category"组合占位，转成单字段 city
            # 让追问文案更自然（任一即可，但用户视角下提示最常见的"城市"即可触发）。
            missing = [m if "|" not in m else "city" for m in missing]
    else:
        missing = _compute_search_missing(intent_result, session)

    if missing:
        # Phase 1（§1.1.2）：写入搜索 awaiting，下一轮裸值优先按字段类型落槽。
        if frame:
            conversation_service.set_search_awaiting(
                session, missing, frame=frame,
            )
        return [_reply(
            msg.from_user,
            _missing_follow_up_text(missing),
            intent=intent_result.intent,
            criteria_snapshot=_snapshot_meta(session),
        )]

    # missing 为空：清搜索 awaiting，进入实际检索
    conversation_service.clear_search_awaiting(session)

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
    # Phase 1（dialogue-intent-extraction-phased-plan §1.4）：搜索 awaiting 兜底。
    # 当 LLM 没抽出任何字段、且当前文本是裸值（如 "2500"）时，把裸值落到 awaiting
    # 队列里第一个语义匹配的字段。LLM 已抽出有效字段时不进这条路径。
    raw_text = (msg.content or "").strip()
    awaiting_consumed = _maybe_consume_search_awaiting_with_bare_value(
        intent_result, raw_text, session,
    )

    # Bug 5：follow_up 走"全量 criteria"语义。
    # LLM 在 prompt 里看得到 current_criteria + 用户这一句，应当输出"应用本句变更后
    # 的完整 criteria 快照"放进 structured_data。这样彻底消解了 add/update 二元选择
    # 带来的歧义（"换成 X" 不再可能被识别为 add 而叠加）。
    #
    # 兼容降级：structured_data 为空时回落到旧的 criteria_patch 合并路径，避免提示词
    # 灰度期间或 LLM 偶发漂移导致 follow_up 完全失效。
    full_criteria = intent_result.structured_data or {}
    if full_criteria:
        conversation_service.replace_criteria(session, full_criteria)
    else:
        conversation_service.merge_criteria_patch(
            session, intent_result.criteria_patch or [],
        )

    # Phase 1：消费 awaiting 字段（无论从 LLM 还是裸值兜底来）
    accepted_keys: list[str] = []
    if awaiting_consumed:
        accepted_keys.extend(awaiting_consumed)
    for k in (full_criteria or {}).keys():
        if k in (session.awaiting_fields or []):
            accepted_keys.append(k)
    if accepted_keys:
        conversation_service.consume_search_awaiting(session, accepted_keys)

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

# ---------------------------------------------------------------------------
# Phase 1：搜索 awaiting helper（dialogue-intent-extraction-phased-plan §1.4）
# ---------------------------------------------------------------------------

# job_search 视角下「askable + 有数值范围可校验」的搜索字段。Phase 1 仅 salary。
# 范围复用 intent_service._SALARY_MIN/_MAX，保证与 _normalize_int_field 一致，
# 避免裸值在两层用不同区间产生不一致裁决（plan §1.4 显式要求复用/对齐）。
_SEARCH_AWAITING_INT_RANGES = {
    "salary_floor_monthly": (_SALARY_MIN, _SALARY_MAX),
    "salary_ceiling_monthly": (_SALARY_MIN, _SALARY_MAX),
}


def _search_frame_for_intent(intent: str | None) -> str | None:
    """search_job/follow_up → job_search；search_worker → candidate_search。"""
    if intent == "search_job":
        return "job_search"
    if intent == "search_worker":
        return "candidate_search"
    return None


def _maybe_consume_search_awaiting_with_bare_value(
    intent_result: IntentResult,
    raw_text: str,
    session: SessionState,
) -> list[str]:
    """裸值兜底落槽：当 LLM 没抽出有效字段时，按 awaiting 队列字段类型匹配裸值。

    返回成功消费的字段列表，并把「应用本轮变更后的完整 criteria 快照」写入
    ``intent_result.structured_data``。

    关键：必须输出 **完整快照**（既有 search_criteria + 新落的 slot），不能只
    返回 partial。下游 ``_handle_follow_up`` 会调 ``replace_criteria`` 全量替换
    session.search_criteria；如果这里只塞 ``{salary_floor_monthly: 2500}``，
    旧的 city/job_category 会被擦掉，正中阶段一要修的"2500 补薪资"场景。
    详见 dialogue-intent-extraction-phased-plan §1.4 "全量快照" 约定，与
    follow_up 的 LLM 输出契约保持一致。

    遵守 §1.4：
      1. awaiting 必须有效（非空 + 未过期）
      2. LLM 已抽出有效 slots_delta 时优先 LLM，不进入裸值兜底
      3. 仅匹配「类型 + 范围」合法的字段，避免 "2500" 被误塞 headcount
      4. 候选字段限定于 awaiting_frame 自身的搜索可追问字段
    """
    # awaiting 已过期或为空：直接清空，避免污染本轮
    if conversation_service.is_search_awaiting_expired(session):
        if session.awaiting_fields or session.awaiting_expires_at:
            conversation_service.clear_search_awaiting(session)
        return []

    # LLM 已抽出有效字段（且不是空 dict）→ 不进入裸值兜底
    if intent_result.structured_data:
        return []

    text = (raw_text or "").strip()
    if not text:
        return []

    # 限定到当前 awaiting_frame：跨 frame 隔离（详见 §1.4）
    awaiting_frame = session.awaiting_frame
    if awaiting_frame not in ("job_search", "candidate_search"):
        return []

    # 按字段类型匹配裸值；当前 Phase 1 仅支持薪资字段（最常见的"2500"场景）。
    # headcount 故意不进入：搜索流程不应出现，避免与上传草稿的 awaiting_field 冲突。
    accepted: list[str] = []
    chosen_field: str | None = None
    chosen_value: int | None = None
    for field in list(session.awaiting_fields or []):
        rng = _SEARCH_AWAITING_INT_RANGES.get(field)
        if not rng:
            continue
        try:
            value = int(text)
        except ValueError:
            continue
        lo, hi = rng
        if value < lo or value > hi:
            continue
        chosen_field = field
        chosen_value = value
        accepted.append(field)
        # 一次裸值最多落一个字段
        break

    if accepted and chosen_field is not None:
        # 全量快照 = 既有 search_criteria 浅拷贝 + 新落的字段。
        # follow_up 主路径会用此 dict 调 replace_criteria，所以这里必须把旧
        # city/job_category/salary 等条件原样保留，避免裸值补槽擦掉上下文。
        snapshot = dict(session.search_criteria or {})
        snapshot[chosen_field] = chosen_value
        intent_result.structured_data = snapshot
        # 仅观测：不带 userid，避免与正式 userid 字段冲突。如未来需要按用户聚合，
        # 由调用方在 message_router._handle_follow_up 内层补上 msg.from_user。
        log_event(
            "search_awaiting_consumed_bare",
            role=session.role,
            frame=awaiting_frame,
            accepted_fields=accepted,
            raw_value=text,
        )
    return accepted


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
    # Phase 1（§1.1.2）：搜索真正执行后清搜索 awaiting，避免下一轮裸值再被旧队列吃掉。
    conversation_service.clear_search_awaiting(session)
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


def _is_field_filled(criteria: dict, field: str) -> bool:
    """判断 criteria 中某字段是否已经有"有效值"。

    - 缺 key / None → 未填
    - 空 list / 空 str / 空 dict → 未填（避免 city=[] 被当作已填）
    - 0 / False → 已填（薪资 0、provide_meal=False 都是合法值）
    """
    if field not in criteria:
        return False
    val = criteria[field]
    if val is None:
        return False
    if isinstance(val, (list, str, dict)) and not val:
        return False
    return True


def _compute_search_missing(
    intent_result: IntentResult,
    session: SessionState,
) -> list[str]:
    """LLM 给的 missing_fields 中，剔除 session.search_criteria 里已有值的字段。

    LLM 在短文本上常误把已知字段标进 missing（例：用户说"西安有吗"，
    session 已有 job_category="餐饮" 但 LLM 仍报 missing=["job_category"]）。

    注意：这里**不**做空 criteria 兜底（min_required）。Stage B P1-1 显式要求
    _handle_search 不在空 criteria 时短路——worker 的简历默认条件需要在下游
    _run_search → _apply_default_criteria 才能注入；最终的安全网由
    search_service.has_effective_search_criteria 把守。
    """
    criteria = session.search_criteria or {}

    seen: set[str] = set()
    result: list[str] = []
    for f in (intent_result.missing_fields or []):
        if f in seen or _is_field_filled(criteria, f):
            continue
        seen.add(f)
        result.append(f)
    return result


def _missing_follow_up_text(missing: list[str], frame: str | None = None) -> str:
    """搜索流程缺字段追问文案，由 slot_schema 模板驱动（阶段三 P2）。

    schema 渲染失败时回退到 upload_service._FIELD_DISPLAY_NAMES + 内联模板，
    避免 schema 不可用时线上回复变空白。
    """
    from app.services.upload_service import _FIELD_DISPLAY_NAMES  # 局部 import 避免 api 层循环
    if not missing:
        return ""
    # frame 兜底：搜索场景默认按 job_search 查 display_name
    effective_frame = frame or "job_search"
    try:
        from app.dialogue import slot_schema as _ss
        text = _ss.render_missing_followup(
            missing, effective_frame, context="search",
            fallback_display=_FIELD_DISPLAY_NAMES,
        )
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    # fallback：与阶段二行为对齐
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
