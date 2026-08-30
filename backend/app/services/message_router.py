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
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings as _settings_module
from app.dialogue import slot_schema
from app.llm.base import IntentResult
from app.llm.prompts import PROMPT_VERSION
from app.models import Resume
from app.schemas.conversation import ReplyMessage, SessionState
from app.schemas.recommendation import (
    ATTEMPT_KIND_BY_REQUEST_KIND,
    RecommendationDeliveryContext,
    RecommendationRequestFact,
)
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
from app.services.recommendation_experience_gate import userid_hash
from app.services.search_permission import (
    ResolvedSearchDirection,
    check_search_permission,
    denied_search_response,
)
from app.services.user_service import UserContext
from app.tasks.common import log_event
from app.wecom.callback import WeComMessage

logger = logging.getLogger(__name__)


def _job_search_facade_enabled(user_ctx: UserContext) -> bool:
    """Return the fail-closed worker rollout decision for the listing facade."""
    if user_ctx.role != "worker" or not getattr(_settings_module, "job_search_facade_enabled", False):
        return False
    percentage = max(0, min(100, int(getattr(_settings_module, "job_search_facade_rollout_percentage", 0))))
    if percentage <= 0:
        return False
    if percentage >= 100:
        return True
    digest = userid_hash(user_ctx.external_userid)
    try:
        bucket = int(digest[:8], 16) % 100
    except (TypeError, ValueError):
        return False
    return bucket < percentage


def _job_search_facade_fallback_reason(user_ctx: UserContext) -> str:
    if not getattr(_settings_module, "job_search_facade_enabled", False):
        return "disabled"
    return "out_of_bucket"


def _render_facade_cards(result, cards) -> None:
    """Replace legacy PII-bearing text only after a valid card projection."""
    if not cards:
        return
    from app.listing.render import render_listing_cards
    result.reply_text = render_listing_cards(cards, has_more=bool(getattr(result, "has_more", False)))
    result.listing_cards = list(cards)


def _experience_flags_for(
    user_ctx: UserContext,
    *,
    direction: str | None = None,
    emit_log: bool = False,
):
    from app.services.recommendation_experience_gate import (
        compute_recommendation_experience_flags,
    )

    return compute_recommendation_experience_flags(
        user_ctx.external_userid,
        direction=direction,
        mode=_settings_module.dialogue_policy.post_search_policy_mode,
        emit_log=emit_log,
    )


# ---------------------------------------------------------------------------
# 固定回复文案
# ---------------------------------------------------------------------------

def _outcome_count(search_outcome, field: str, default: int | None = 0) -> int | None:
    value = getattr(search_outcome, field, default)
    if value is None:
        return default
    return int(value)


def _log_post_search_decision(
    *,
    mode: str,
    user_ctx: UserContext,
    ps_decision,
    search_outcome,
) -> None:
    log_event(
        "post_search_decision",
        external_userid_hash=userid_hash(user_ctx.external_userid),
        mode=mode,
        action=ps_decision.action,
        decision=ps_decision.action,
        reasoning=ps_decision.reasoning,
        direction=search_outcome.direction,
        snapshot_exhausted=search_outcome.snapshot_exhausted,
        initial_count=search_outcome.initial_count,
        initial_visible_count=_outcome_count(
            search_outcome, "visible_count", search_outcome.initial_count,
        ),
        final_count=search_outcome.final_count,
        final_visible_count=_outcome_count(
            search_outcome, "visible_count", search_outcome.final_count,
        ),
        shown_count=_outcome_count(search_outcome, "shown_count", None),
        remaining_count_capped=_outcome_count(
            search_outcome, "remaining_count_capped", None,
        ),
        desired_count=search_outcome.desired_count,
        applied_relax_step=search_outcome.applied_relax_step,
        soft_pref_hits=dict(search_outcome.soft_pref_hits or {}),
    )


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
PENDING_MAX_ROUNDS_REPLY = (
    "信息还没识别完整，草稿已为您保留。"
    "请直接发送缺少字段的值，或回复“取消草稿”放弃。"
)
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
CONFLICT_DEAD_LOOP_REPLY = (
    "未识别您的选择，已保留原草稿并继续发布。"
    "请补充尚缺字段，或回复“取消草稿”放弃。"
)
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
# codex review 修订（PR4 P1-2）：LLM 显式 needs_clarification=True 但 reducer 自身
# 没决定具体 clarify kind 时，用通用文案让用户提供更多信息。
_V2_CLAR_LLM_REQUESTED = (
    "您的描述我没完全理解，方便再说得具体一些吗？比如城市、岗位类型、薪资期望等。"
)

# 放宽确认是系统刚刚给出的二选一，允许用精确闭集在 LLM 不可用或 v2 关闭时
# 完成状态机。这里只做整句匹配，禁止 substring 命中，避免把“不要取消搜索”
# 之类的否定复句误判为拒绝。
_RELAXATION_ACCEPT_EXACT = frozenset({
    "好", "好的", "可以", "行", "同意", "确认", "放宽", "是",
})
_RELAXATION_REJECT_EXACT = frozenset({
    "不", "不要", "不用", "不可以", "算了", "取消", "保持原条件", "否",
})
# codex review 修订（PR4 P1-3）：脏 slots_delta 全被 schema drop 掉但本轮需要业务动作时,
# 不再静默继续旧搜索条件，反问让用户重新表达。
_V2_CLAR_DROPPED_SLOTS_NO_VALID = (
    "您说的字段我没识别出来，方便用更标准的方式再描述一次吗？比如城市、工种、薪资。"
)
_V2_CLAR_DEFAULT = "请再说得具体一些，方便我帮您处理。"
COMPLEX_ACTION_CLARIFICATION_REPLY = (
    "我识别到您这句话里有多个先后操作。为避免执行错，请一次说一个："
    "先告诉我现在要做的第一件事；完成后再发下一件。当前会话内容已保留。"
)
PENDING_ACTION_SAVED_REPLY = (
    "我先处理第一件事，并已记住下一步：{action}\n"
    "完成当前操作后，可回复 /下一步 查看，或直接发送这句话执行；"
    "回复 /取消下一步 可删除。"
)
PENDING_ACTION_VIEW_REPLY = "已保存的下一步是：{action}\n直接发送这句话即可执行。"
PENDING_ACTION_NONE_REPLY = "当前没有已保存的下一步。"
PENDING_ACTION_CANCELLED_REPLY = "已取消保存的下一步。"
PENDING_ACTION_WAIT_REPLY = "请先完成或取消当前发布流程，再执行已保存的下一步。"
PENDING_ACTION_EXISTS_REPLY = (
    "当前已经保存了一项下一步，请先执行或回复 /取消下一步，再安排新的组合操作。"
)
_PENDING_ACTION_TTL_SECONDS = 30 * 60
_ACTION_PLAN_SPLIT_RE = re.compile(
    r"\s*(?:，|,|；|;)?\s*(?:完成后再|不行再|同时还|另外再|然后|接着|顺便|"
    r"再(?=(?:帮|给|找|看|发布|提交|登记|换)))\s*"
)


# Phase 5 §5.2：turn-scoped 上下文 holder。_handle_text 在 v2_dual_read / primary
# 路径下把真实 parse_result / decision 存进来，_post_search_dispatch 读取后让
# reducer 看到准确的 accepted_slots_delta / confidence；turn 结束清空。
#
# 第 8 轮 review fix 2：用 contextvars.ContextVar 替换 module-level dict。
# 当前 worker 主循环是单线程串行（worker.py:_main_loop），但 contextvars 跨线程 /
# 跨 asyncio task 默认隔离，对未来扩展（ThreadPoolExecutor 并行 / pytest-xdist
# 并行测试）友好。注释里写"单线程处理"= 把并发约束埋在代码注释里，半年后没人会记得。
from contextvars import ContextVar as _ContextVar

_v2_parse_result: _ContextVar = _ContextVar("_v2_parse_result", default=None)
_v2_decision: _ContextVar = _ContextVar("_v2_decision", default=None)

# §9.4 `recommendation_request.total_latency_ms`：请求总耗时从本轮第一次真实检索
# 开始计时。自动放宽会在同一 request 内跑第二次检索，所以只在检索入口 start 一次，
# applier 递归时不重置。
_recommendation_clock: _ContextVar = _ContextVar("_recommendation_clock", default=None)


def _start_recommendation_clock() -> None:
    """在真正发起检索前打点；同一 turn 内重复调用只保留最早一次。"""
    if _recommendation_clock.get() is None:
        _recommendation_clock.set(time.monotonic())


def _reset_recommendation_clock() -> None:
    _recommendation_clock.set(None)


def _recommendation_elapsed_ms() -> int:
    started = _recommendation_clock.get()
    if started is None:
        return 0
    return max(0, int((time.monotonic() - started) * 1000))


def _set_v2_turn_context(parse_result, decision) -> None:
    _v2_parse_result.set(parse_result)
    _v2_decision.set(decision)


def _clear_v2_turn_context() -> None:
    _v2_parse_result.set(None)
    _v2_decision.set(None)


def _is_relaxation_expired(iso_str: str) -> bool:
    """Phase 5 §第 8 轮 review fix 1：判断 pending_relaxation.expires_at 是否过期。

    与 upload_service.is_pending_upload_expired 同款防御：
    1. 解析失败按已过期处理（脏数据不卡流程）。
    2. naive datetime 补 UTC tzinfo 避免与 aware now 比较抛 TypeError。
    """
    from datetime import datetime as _dt, timezone as _tz
    if not iso_str:
        return False
    try:
        expires = _dt.fromisoformat(iso_str)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_tz.utc)
        return _dt.now(_tz.utc) > expires
    except (TypeError, ValueError):
        return True


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
    if kind == "llm_requested":
        return _V2_CLAR_LLM_REQUESTED
    if kind == "dropped_slots_no_valid":
        return _V2_CLAR_DROPPED_SLOTS_NO_VALID
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

def process(msg: WeComMessage, db: Session, action_context=None) -> list[ReplyMessage]:
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
    # Phase 5 §5.2：用 try/finally 清空 turn-scoped v2 context，避免跨 turn 泄漏。
    _reset_recommendation_clock()
    try:
        mtype = msg.msg_type or ""
        if mtype == "text":
            return _handle_text(msg, user_ctx, db, action_context=action_context)
        if mtype == "image":
            return _handle_image(msg, user_ctx, db)
        if mtype == "voice":
            return [_reply(userid, VOICE_NOT_SUPPORTED)]
        if mtype == "event":
            logger.info(
                "message_router: wecom event received user_hash=%s",
                userid_hash(userid),
            )
            return []
        if mtype in ("file", "video", "link", "location"):
            return [_reply(userid, FILE_NOT_SUPPORTED)]
        # 未知类型兜底
        logger.warning(
            "message_router: unknown msg_type=%s user_hash=%s",
            mtype,
            userid_hash(userid),
        )
        return [_reply(userid, UNKNOWN_TYPE_REPLY)]
    finally:
        _clear_v2_turn_context()
        _reset_recommendation_clock()


# ---------------------------------------------------------------------------
# 文本链路
# ---------------------------------------------------------------------------

def _handle_text(
    msg: WeComMessage,
    user_ctx: UserContext,
    db: Session,
    *,
    action_context=None,
) -> list[ReplyMessage]:
    userid = msg.from_user
    content = (msg.content or "").strip()

    # 加载 / 创建 session
    session = conversation_service.load_session(userid)
    if session is None:
        session = conversation_service.create_session(userid, user_ctx.role)
    # Stage C1：兜底推导 + self-heal，覆盖测试或非 Redis 路径绕过 load_session 的场景
    conversation_service.ensure_active_flow(session)

    # Upload TTL is a routing precondition. It must run before welcome,
    # action-plan and V2 clarification/conflict short returns so no branch can
    # preserve an expired draft or its media references.
    if _abandon_expired_pending_upload(session, db):
        if _looks_like_upload_patch(content):
            conversation_service.record_history(session, "user", content)
            conversation_service.record_history(
                session, "assistant", PENDING_EXPIRED_REPLY,
            )
            conversation_service.save_session(userid, session)
            return [_reply(userid, PENDING_EXPIRED_REPLY)]

    # Any subsequent text turn closes the post-upload attachment window before
    # welcome, clarification, V2 reset, or classifier failure can short-return.
    # A complete successful upload in this same turn writes a new exact target.
    if not session.pending_upload_intent:
        session.attachment_target_type = None
        session.attachment_target_id = None

    # 空文本也必须关闭附件窗口并持久化；不能绕过上面的 TTL/target 清理。
    if not content:
        conversation_service.save_session(userid, session)
        return [_reply(userid, FALLBACK_REPLY)]

    # 首次欢迎优先于意图分类
    if user_ctx.should_welcome:
        conversation_service.record_history(session, "user", content)
        welcome = _build_welcome(user_ctx)
        conversation_service.record_history(session, "assistant", welcome)
        conversation_service.save_session(userid, session)
        return [_reply(userid, welcome)]

    # 受限两动作计划：第二动作只保存原文，不提前分类或执行。旧 session 没有该字段
    # 时 Pydantic 默认 None；到期自动清理，不让陈旧动作跨会话误触发。
    if _is_pending_action_expired(session.pending_action):
        session.pending_action = None
    if content == "/取消下一步":
        had_pending = bool(session.pending_action)
        session.pending_action = None
        reply_text = (
            PENDING_ACTION_CANCELLED_REPLY if had_pending else PENDING_ACTION_NONE_REPLY
        )
        conversation_service.record_history(session, "user", content)
        conversation_service.record_history(session, "assistant", reply_text)
        conversation_service.save_session(userid, session)
        return [_reply(userid, reply_text)]
    if content == "/下一步":
        action = (session.pending_action or {}).get("raw_text")
        reply_text = (
            PENDING_ACTION_VIEW_REPLY.format(action=action)
            if action else PENDING_ACTION_NONE_REPLY
        )
        conversation_service.record_history(session, "user", content)
        conversation_service.record_history(session, "assistant", reply_text)
        conversation_service.save_session(userid, session)
        return [_reply(userid, reply_text)]

    consume_pending_action = False
    pending_raw = str((session.pending_action or {}).get("raw_text") or "").strip()
    if pending_raw and content == pending_raw:
        if session.active_flow in {"upload_collecting", "upload_conflict"}:
            conversation_service.record_history(session, "user", content)
            conversation_service.record_history(
                session, "assistant", PENDING_ACTION_WAIT_REPLY,
            )
            conversation_service.save_session(userid, session)
            return [_reply(userid, PENDING_ACTION_WAIT_REPLY)]
        # 第二动作是独立动作，不继承第一项搜索的条件、分页或放宽确认。
        session.search_criteria = {}
        session.last_criteria = {}
        session.candidate_snapshot = None
        session.shown_items = []
        session.pending_relaxation = None
        conversation_service.clear_search_awaiting(session)
        session.active_flow = "idle"
        consume_pending_action = True

    deferred_action_notice: str | None = None

    def _finalize_action_plan_replies(
        replies: list[ReplyMessage],
    ) -> list[ReplyMessage]:
        """Apply pending-action bookkeeping on both normal and short-return paths."""
        nonlocal consume_pending_action
        if replies and deferred_action_notice:
            replies[0].content = (
                f"{replies[0].content}\n\n{deferred_action_notice}"
            )
        if consume_pending_action:
            session.pending_action = None
            consume_pending_action = False
        return replies

    action_plan = _extract_bounded_action_plan(content)
    if action_plan is not None:
        if session.pending_action and not consume_pending_action:
            conversation_service.record_history(session, "user", content)
            conversation_service.record_history(
                session, "assistant", PENDING_ACTION_EXISTS_REPLY,
            )
            conversation_service.save_session(userid, session)
            return [_reply(userid, PENDING_ACTION_EXISTS_REPLY)]
        first_action, second_action = action_plan
        now = datetime.now(timezone.utc)
        session.pending_action = {
            "raw_text": second_action,
            "created_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(
                now.timestamp() + _PENDING_ACTION_TTL_SECONDS,
                tz=timezone.utc,
            ).isoformat(),
        }
        content = first_action
        msg = dataclasses.replace(msg, content=first_action)
        deferred_action_notice = PENDING_ACTION_SAVED_REPLY.format(
            action=second_action,
        )

    # 先把当前用户消息写入 history，再让 LLM 看到完整上下文
    conversation_service.record_history(session, "user", content)

    # 生产降级保护：pending_relaxation 是系统主动给出的二选一上下文。
    # 即使 dialogue_v2_mode=off、provider 超时或 v2 fallback，精确短回答也应完成
    # 确认闭环，而不是落成 chitchat 并让 pending 状态悬挂。
    relaxation_transition = _match_pending_relaxation_response(content, session)
    if relaxation_transition is not None:
        from app.services.dialogue_reducer import DialogueDecision

        pending = dict(session.pending_relaxation or {})
        frame = pending.get("frame")
        if frame not in {"job_search", "candidate_search"}:
            frame = (
                "job_search"
                if pending.get("direction") == "search_job"
                else "candidate_search"
            )
        decision = DialogueDecision(
            dialogue_act="respond_relaxation_offer",
            resolved_frame=frame,
            route_intent="follow_up",
            state_transition=relaxation_transition,
        )
        replies = _route_v2_relaxation_response(
            decision, msg, user_ctx, session, db, action_context=action_context,
        )
        replies = _finalize_action_plan_replies(replies)
        if replies:
            _record_reply_history(session, replies[0])
        conversation_service.save_session(userid, session)
        return replies

    # 当前 DTO/状态机只承诺单个主动作。空闲/搜索态遇到显式顺序或“顺便”组合时，
    # 不让 LLM 猜执行顺序；上传态仍交给既有 upload_conflict 闭环处理。
    if (
        session.active_flow not in {"upload_collecting", "upload_conflict"}
        and _requires_action_plan_clarification(content)
    ):
        conversation_service.record_history(
            session, "assistant", COMPLEX_ACTION_CLARIFICATION_REPLY,
        )
        conversation_service.save_session(userid, session)
        return [_reply(userid, COMPLEX_ACTION_CLARIFICATION_REPLY)]

    # 阶段二（dialogue-intent-extraction-phased-plan §2.3）：
    # mode=off 时直接走 legacy classify_intent 路径，保持已有调用点 / 测试兼容；
    # mode=shadow / dual_read 时走 classify_dialogue 入口，里面再决定要不要旁路 v2。
    v2_mode = getattr(_settings_module, "dialogue_v2_mode", "off")
    decision = None  # type: ignore[assignment]
    source = "legacy"

    if action_context is not None and getattr(action_context, "intent_result", None) is not None:
        # ActionGateway already performed the single allowed parse for this turn.
        intent_result = action_context.intent_result
        decision = None
        source = "legacy_from_parse"
    elif v2_mode == "off":
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
            replies = _finalize_action_plan_replies(
                [_reply(userid, SYSTEM_BUSY_REPLY)],
            )
            conversation_service.save_session(userid, session)
            return replies
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
            replies = _finalize_action_plan_replies(
                [_reply(userid, SYSTEM_BUSY_REPLY)],
            )
            conversation_service.save_session(userid, session)
            return replies
        intent_result = route.intent_result
        decision = route.decision
        source = route.source

        # 阶段二 v2 分支：dual_read 命中 / 阶段四 PR3 primary 命中
        # 共用 v2 派生路径：apply_awaiting_ops + clarification 短路 + state_transition
        # 消费 + apply_decision。fallback 路径（v2_fallback_legacy / v2_primary_fallback_legacy）
        # 走下面的 legacy 路由，与 source=="legacy" 等价处理。
        if source in {"v2_dual_read", "v2_primary"} and decision is not None:
            # Phase 5 §5.2：把真实 parse_result + decision 暂存，让
            # _post_search_dispatch 在搜索后能读到。turn 结束（return 前后）需要清。
            # parse_result 由 intent_service.classify_dialogue 在 DialogueRouteResult
            # 中透传，让 _decide_zero_result 的低置信度规则在真实链路也能命中。
            _set_v2_turn_context(
                parse_result=getattr(route, "parse_result", None),
                decision=decision,
            )
            from app.services.dialogue_applier import apply_awaiting_ops, apply_decision
            # awaiting_ops 必须在所有 v2 分支上执行（包括 clarification / 冲突短路），
            # 否则被消费的 awaiting 字段会僵尸保留（adversarial review C1/I15）。
            apply_awaiting_ops(decision, session)
            if decision.clarification:
                # 直接渲染反问，不走 _route_*
                reply_text = _render_v2_clarification(decision.clarification, session)
                replies = _finalize_action_plan_replies(
                    [_reply(userid, reply_text)],
                )
                _record_reply_history(session, replies[0])
                conversation_service.save_session(userid, session)
                return replies
            # enter_upload_conflict：直接调现成的 _enter_upload_conflict
            # 既写状态又生成 CONFLICT_PROMPT_FMT，避免在 applier 里复制冲突文案逻辑。
            if decision.state_transition == "enter_upload_conflict":
                replies = _enter_upload_conflict(intent_result, msg, session)
                replies = _finalize_action_plan_replies(replies)
                if replies:
                    _record_reply_history(session, replies[0])
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
                apply_decision(
                    decision, session, msg=msg, intent_result=intent_result, db=db,
                )
                replies = _route_v2_resolve_conflict(
                    decision, msg, user_ctx, session, db,
                )
                replies = _finalize_action_plan_replies(replies)
                if replies:
                    _record_reply_history(session, replies[0])
                conversation_service.save_session(userid, session)
                return replies
            # Phase 5 §5.2.1.5 执行归属表 / §5.2.3 message_router 行：
            # respond_relaxation_offer short-circuit（与 resolve_conflict 平行）。
            # apply_decision 调用边界：仅当 state_transition=clear_pending_relaxation
            # 时调（applier 处理 session-only 清状态）；apply_relaxation /
            # cancel_relaxation **跳过** apply_decision，由 _route_v2_relaxation_response
            # 接管二次检索 + 清状态。
            if decision.dialogue_act == "respond_relaxation_offer":
                if decision.state_transition == "clear_pending_relaxation":
                    apply_decision(
                        decision, session, msg=msg, intent_result=intent_result, db=db,
                    )
                replies = _route_v2_relaxation_response(
                    decision, msg, user_ctx, session, db, action_context=action_context,
                )
                replies = _finalize_action_plan_replies(replies)
                if replies:
                    _record_reply_history(session, replies[0])
                conversation_service.save_session(userid, session)
                return replies
            # codex review 修订（PR4 P1-1）：cancel / reset 走专用 handler，**不再
            # 落到下面的通用 command 路由**。原因：v2 reducer 把 cancel/reset 翻译为
            # state_transition=clear_pending_upload/reset_search，applier 会先清 session,
            # 再走通用 command handler 时 command_service 看到的已经是清空后 session,
            # 反向输出「当前没有可取消/可清空」。专用 handler 在 apply_decision **之前**
            # 快照 pre-state，apply_decision **之后** 基于 pre-state 渲染准确文案 short-return。
            if decision.dialogue_act in {"cancel", "reset"}:
                pre_state = {
                    "had_pending_upload": bool(session.pending_upload_intent),
                    "had_search_state": bool(
                        session.search_criteria
                        or session.candidate_snapshot is not None
                        or session.shown_items
                    ),
                    "active_flow": session.active_flow,
                }
                apply_decision(
                    decision, session, msg=msg, intent_result=intent_result, db=db,
                )
                replies = _route_v2_cancel_reset(decision, pre_state, msg, session)
                replies = _finalize_action_plan_replies(replies)
                if replies:
                    _record_reply_history(session, replies[0])
                conversation_service.save_session(userid, session)
                return replies
            # 其它 transition → applier 物化（awaiting_ops 已经 apply 过，applier 内部
            # 重复调用也是幂等的：consume_search_awaiting 对已消费字段是 no-op）。
            apply_decision(
                decision, session, msg=msg, intent_result=intent_result, db=db,
            )

    if user_ctx.role == "broker":
        from app.services.dialogue_reducer import broker_explicit_direction

        anchored_direction = broker_explicit_direction(content)
        if (
            anchored_direction is not None
            and intent_result.intent in {
                "search_job", "search_worker", "follow_up", "chitchat",
            }
        ):
            if (
                session.broker_direction in {"search_job", "search_worker"}
                and session.broker_direction != anchored_direction
            ):
                # A legacy/fallback provider can call an explicit object switch a
                # follow-up. Reset all direction-scoped state before dispatch so
                # candidate criteria/snapshots never leak into job search or vice
                # versa.
                session.search_criteria = {}
                session.last_criteria = {}
                session.candidate_snapshot = None
                session.shown_items = []
                session.pending_relaxation = None
                conversation_service.clear_search_awaiting(session)
                session.active_flow = "idle"
            anchored_data = dict(intent_result.structured_data or {})
            anchored_data.update(
                intent_service.extract_explicit_search_anchors(content),
            )
            if anchored_direction == "search_job":
                ceiling = anchored_data.pop("salary_ceiling_monthly", None)
                if ceiling is not None:
                    anchored_data.setdefault("salary_floor_monthly", ceiling)
            else:
                floor = anchored_data.pop("salary_floor_monthly", None)
                if floor is not None:
                    anchored_data.setdefault("salary_ceiling_monthly", floor)
            session.broker_direction = anchored_direction
            intent_result = intent_result.model_copy(update={
                "intent": anchored_direction,
                "structured_data": anchored_data,
                "missing_fields": [],
            })

    intent = intent_result.intent

    # Stage C1（spec §2.5）：last_intent 仅观测；current_intent 在 upload_collecting
    # 期间钉在 pending_upload_intent，供对话语义与观测使用。
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
            action_context=action_context,
        )
    else:
        replies = _route_idle(
            intent_result, msg, user_ctx, session, db,
            action_context=action_context,
        )

    # 把出站回复写入 history（只记第一条，避免历史爆炸）
    replies = _finalize_action_plan_replies(replies)
    if replies:
        _record_reply_history(session, replies[0])

    conversation_service.save_session(userid, session)
    return replies


def _dispatch_intent(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
    *,
    action_context=None,
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
            return _handle_show_more(
                msg, user_ctx, session, db, action_context=action_context,
            )
        if intent == "chitchat":
            return [_reply(userid, _chitchat_text(user_ctx))]
        # 未知意图兜底
        logger.warning(
            "message_router: unknown intent=%s user_hash=%s",
            intent,
            userid_hash(userid),
        )
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
    *,
    action_context=None,
) -> list[ReplyMessage]:
    """idle 状态：复用现有 _dispatch_intent 即可；upload/search handler 内部
    会按需把 active_flow 推进到 upload_collecting / search_active。"""
    return _dispatch_intent(
        intent_result, msg, user_ctx, session, db,
        action_context=action_context,
    )


def _route_search_active(
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
    *,
    action_context=None,
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
        return _dispatch_intent(
            intent_result, msg, user_ctx, session, db,
            action_context=action_context,
        )

    if intent == "chitchat":
        return [_reply(userid, _chitchat_text(user_ctx))]

    return _dispatch_intent(
        intent_result, msg, user_ctx, session, db,
        action_context=action_context,
    )


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

    return _handle_command_intent(intent_result, user_ctx, session, db, msg=msg)


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
    if _abandon_expired_pending_upload(session, db):
        was_patch = _looks_like_upload_patch(content)
        if was_patch:
            return [_reply(userid, PENDING_EXPIRED_REPLY)]
        # 未补丁就放行到 idle 分发
        return _route_idle(intent_result, msg, user_ctx, session, db)

    # 2. cancel 强规则
    if _is_cancel(content, intent_result):
        upload_service.abandon_pending_upload(session, db)
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
      - 其他 → 重复确认；连续 2 次仍未识别则放弃新意图并恢复原草稿
    """
    content = (msg.content or "").strip()
    userid = msg.from_user
    intent = intent_result.intent

    if _abandon_expired_pending_upload(session, db):
        if _looks_like_upload_patch(content):
            return [_reply(userid, PENDING_EXPIRED_REPLY)]
        return _route_idle(intent_result, msg, user_ctx, session, db)

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
        upload_service.abandon_pending_upload(session, db)
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
        upload_service.abandon_pending_upload(session, db)
        forwarded = _route_idle(new_intent_result, forwarded_msg, user_ctx, session, db)
        return [_reply(userid, CONFLICT_PROCEED_ACK)] + forwarded

    # 死循环防护：用户没有明确选择时绝不丢弃草稿。连续两次未识别后，
    # 放弃本次 interruption，回到原上传流程；草稿仍由显式取消或 TTL 清理。
    session.conflict_followup_rounds += 1
    if session.conflict_followup_rounds >= 2:
        session.pending_interruption = None
        session.conflict_followup_rounds = 0
        session.active_flow = "upload_collecting"
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
        upload_service.abandon_pending_upload(session, db)
        session.pending_interruption = None

        forwarded = _route_idle(new_intent_result, forwarded_msg, user_ctx, session, db)
        return [_reply(userid, CONFLICT_PROCEED_ACK)] + forwarded

    # 兜底（理论上不该走到这里 —— reducer 不会输出其它 transition for resolve_conflict）
    logger.warning(
        "_route_v2_resolve_conflict: unexpected transition=%s, falling back to UNKNOWN",
        transition,
    )
    return [_reply(userid, FALLBACK_REPLY)]


def _route_v2_relaxation_response(
    decision,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
    *,
    action_context=None,
) -> list[ReplyMessage]:
    """Phase 5 §5.2.1.5 执行归属表 / §5.2.3 message_router 行：放宽确认派发。

    与 ``_route_v2_resolve_conflict`` **平行**（不复用其函数体），处理用户对
    "要把 X 放宽吗"反问的回应：
    - ``apply_relaxation`` → 调 ``execute_relaxed_search(original, step, ...)``
      拿放宽后 ``(SearchResult, SearchOutcome)``，构造 PostSearchContext
      (recursion_depth=1) 走二阶段 reducer + applier；
    - ``cancel_relaxation`` → 用模板渲染"好的，那我们换其他条件"。

    无论哪条分支，**函数自身**在执行后清 ``session.pending_relaxation``。

    P1 评审硬约束（详见 phased-plan §5.2.4 验收 #6 / #8）：
    - **只读 ``original_criteria``**（不读 relaxed_criteria，避免二次放宽）；
    - **raw_query 从 pending 读取**（不用 ``msg.content``——确认轮通常是
      "好的/可以"，作为 reranker query 会让排序退化）。
    """
    userid = msg.from_user
    transition = decision.state_transition
    pending = dict(session.pending_relaxation or {})

    # 兜底：pending_relaxation 缺失时不应走到这里（_handle_text 应该不会路由），
    # 但防御一下。
    if not pending:
        logger.warning(
            "_route_v2_relaxation_response: pending_relaxation is None; "
            "falling back to chitchat reply",
        )
        return [_reply(userid, "好的。")]

    # 第 8 轮 review fix 1：检查 pending_relaxation.expires_at。
    # TTL 在 applier 写入时由 search_awaiting_ttl_seconds 决定，过期后用户回话
    # 不应再被识别为"接受放宽"——上下文已模糊。
    expires_at = pending.get("expires_at")
    if expires_at and _is_relaxation_expired(expires_at):
        log_event(
            "pending_relaxation_expired",
            userid=userid,
            step=pending.get("step"),
            expires_at=expires_at,
        )
        session.pending_relaxation = None
        return [_reply(userid, "刚才的搜索话题已过期，重新开始搜索吧。")]

    if transition == "cancel_relaxation":
        session.pending_relaxation = None
        return [_reply(userid, "好的，那我们换其他条件重新搜索。")]

    if transition == "apply_relaxation":
        from app.services.post_search_applier import apply_post_search_decision
        from app.services.post_search_reducer import (
            PostSearchContext,
            post_search_reduce,
        )
        from app.services import search_service as _search_service
        from app.llm.base import DialogueParseResult
        from app.services.dialogue_reducer import DialogueDecision

        # phased-plan §5.2.1.5 third row：只读 original_criteria + 持久化的
        # raw_query / user_msg_id；**不**读 relaxed_criteria；**不**用 msg.content。
        original_criteria = dict(pending.get("original_criteria") or {})
        step = pending.get("step") or ""
        direction = pending.get("direction") or "search_job"
        permission_decision = check_search_permission(
            user_ctx,
            direction,
            entrypoint="message_router.confirmed_relaxation",
            request_id=msg.msg_id,
        )
        if not permission_decision.allowed:
            session.pending_relaxation = None
            denied_result, _ = denied_search_response(permission_decision)
            return [_reply(userid, denied_result.reply_text)]
        persisted_raw_query = pending.get("raw_query") or ""
        persisted_user_msg_id = pending.get("user_msg_id")
        original_visible_count = int(pending.get("original_visible_count") or 0)
        experience_flags = _experience_flags_for(
            user_ctx, direction=direction, emit_log=True,
        )

        _start_recommendation_clock()
        if direction == "search_job" and _job_search_facade_enabled(user_ctx):
            try:
                from app.listing.search import JobSearchFacade, SearchTurn
                facade_response = JobSearchFacade(db, enabled=True).relax_search(
                    user_ctx, session,
                    SearchTurn(
                        raw_query=persisted_raw_query,
                        user_msg_id=persisted_user_msg_id,
                        snapshot_id=getattr(session.candidate_snapshot, "snapshot_id", None),
                    ),
                    step, db=db, confirmed=True,
                    experience_flags=experience_flags,
                )
                new_result, new_outcome = facade_response.result, facade_response.outcome
                if facade_response.used_facade:
                    _render_facade_cards(new_result, facade_response.cards)
            except Exception as exc:
                log_event(
                    "facade_fallback", direction="search_job", action="relax_search",
                    reason=type(exc).__name__, user_msg_id=persisted_user_msg_id,
                )
                new_result, new_outcome = _search_service.execute_relaxed_search(
                    original_criteria, step, direction=direction,
                    raw_query=persisted_raw_query, session=session, user_ctx=user_ctx,
                    db=db, user_msg_id=persisted_user_msg_id,
                    experience_flags=experience_flags,
                    original_visible_count=original_visible_count,
                )
        else:
            if direction == "search_job":
                log_event(
                    "facade_fallback", direction="search_job", action="relax_search",
                    reason=_job_search_facade_fallback_reason(user_ctx),
                    user_msg_id=persisted_user_msg_id,
                )
            new_result, new_outcome = _search_service.execute_relaxed_search(
                original_criteria, step, direction=direction,
                raw_query=persisted_raw_query, session=session, user_ctx=user_ctx,
                db=db, user_msg_id=persisted_user_msg_id,
                experience_flags=experience_flags,
                original_visible_count=original_visible_count,
            )

        # 二阶段 reducer
        parse_stub = DialogueParseResult(
            dialogue_act="chitchat",
            frame_hint="none",
            slots_delta={},
            merge_hint={},
            needs_clarification=False,
            confidence=1.0,
        )
        decision_stub = DialogueDecision(
            dialogue_act="chitchat",
            resolved_frame="none",
            route_intent="follow_up",
        )
        new_ps_decision = post_search_reduce(
            parse_result=parse_stub,
            decision=decision_stub,
            session=session,
            search_outcome=new_outcome,
            role=user_ctx.role,
            experience_flags=experience_flags,
        )
        # 第二轮 reducer 必须不再输出 auto_relax_and_retry
        # 第 8 轮 review fix 3：用 raise 而非 assert（-O 模式会剥）
        if new_ps_decision.action == "auto_relax_and_retry":
            raise RuntimeError(
                "post_search_reduce produced auto_relax_and_retry on second pass "
                "via _route_v2_relaxation_response"
            )

        ctx = PostSearchContext(
            decision=new_ps_decision,
            search_result=new_result,
            search_outcome=new_outcome,
            parse_result=parse_stub,
            dialogue_decision=decision_stub,
            session=session,
            msg=msg,
            user_ctx=user_ctx,
            db=db,
            raw_query=persisted_raw_query,
            role=user_ctx.role,
            experience_flags=experience_flags,
            recursion_depth=1,
        )
        replies = apply_post_search_decision(ctx)
        # 用户确认放宽后的这次检索是新的 confirmed_relaxed request（§9.4）；
        # 请求事实必须写（含零结果），delivery 只挂给真渲染了候选的那条回复。
        recommendation_fields = _recommendation_reply_fields(
            new_result,
            user_ctx.external_userid,
            msg.msg_id,
            request_kind="confirmed_relaxed",
            parent_request_id=pending.get("parent_request_id"),
            search_outcome=new_outcome,
        )
        replies = _attach_recommendation_fields(
            replies, recommendation_fields, new_result,
        )

        # 函数自身清 pending_relaxation（不依赖 apply_decision）
        session.pending_relaxation = None

        # 推进 active_flow（与 _handle_show_more 相同语义）
        if session.candidate_snapshot is not None:
            session.active_flow = "search_active"
        return replies

    # 兜底
    logger.warning(
        "_route_v2_relaxation_response: unexpected transition=%s",
        transition,
    )
    session.pending_relaxation = None
    return [_reply(userid, "好的。")]


def _match_pending_relaxation_response(
    content: str,
    session: SessionState,
) -> str | None:
    """精确识别系统二选一后的短回答；无 pending 时绝不生效。"""
    if not session.pending_relaxation:
        return None
    normalized = (content or "").strip().rstrip("。！!，,").strip().lower()
    if normalized in _RELAXATION_ACCEPT_EXACT:
        return "apply_relaxation"
    if normalized in _RELAXATION_REJECT_EXACT:
        return "cancel_relaxation"
    return None


def _extract_bounded_action_plan(content: str) -> tuple[str, str] | None:
    """Extract exactly two explicit sequential actions; never infer omitted actions."""
    text = (content or "").strip()
    if not text:
        return None
    parts = [part.strip(" ，,；;") for part in _ACTION_PLAN_SPLIT_RE.split(text)]
    parts = [part for part in parts if part]
    if len(parts) != 2:
        return None
    first, second = parts
    if first.startswith("先"):
        first = first[1:].strip()
    if len(first) < 2 or len(second) < 2:
        return None
    return first, second


def _requires_action_plan_clarification(content: str) -> bool:
    """Reject three-plus or ambiguous action plans that cannot be safely bounded."""
    text = (content or "").strip()
    if not text or _extract_bounded_action_plan(text) is not None:
        return False
    parts = [part for part in _ACTION_PLAN_SPLIT_RE.split(text) if part.strip()]
    if len(parts) >= 3:
        return True
    return bool(
        any(marker in text for marker in ("顺便", "同时还", "另外再", "完成后再"))
        or re.search(r"先.+?(?:然后|接着|不行再|再).+", text)
    )


def _is_pending_action_expired(pending: dict | None) -> bool:
    if not pending:
        return False
    raw = pending.get("expires_at")
    if not raw:
        return True
    try:
        expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expires.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return True


def _route_v2_cancel_reset(
    decision,
    pre_state: dict,
    msg: WeComMessage,
    session: SessionState,
) -> list[ReplyMessage]:
    """阶段四 PR4 codex review P1-1 修复：v2 cancel / reset 专用 handler。

    与 _route_v2_resolve_conflict 同模式：调用方先 apply_decision 物化 session 状态,
    本函数基于 **pre-apply 快照** 渲染准确文案。**不**走通用 command 路由,
    避免 command_service 看到的已经是清空后 session 反向输出「当前没有可取消」。

    cancel：
    - pre-apply 有 pending_upload_intent → 「已取消，岗位草稿已丢弃。」
    - pre-apply 无 pending_upload → 「当前没有可取消的草稿。」（与 legacy 行为对齐）

    reset：
    - pre-apply 有 pending_upload + 有 search_state → 「搜索条件已重置；您仍在发布{kind}」
    - pre-apply 有 pending_upload 但无 search_state → 「您仍在发布{kind}（缺{field}）」
      （与 legacy _handle_reset_search 同形）
    - pre-apply 有 search_state（无 pending）→ 「已帮您清空当前搜索条件和结果」
    - pre-apply 无 search_state 也无 pending → 「当前没有可清空的搜索条件。」
    """
    userid = msg.from_user
    act = decision.dialogue_act

    if act == "cancel":
        if pre_state.get("had_pending_upload"):
            return [_reply(userid, command_service.CANCEL_PENDING_OK)]
        return [_reply(userid, command_service.CANCEL_PENDING_NO_DRAFT)]

    if act == "reset":
        had_pending = pre_state.get("had_pending_upload")
        had_search = pre_state.get("had_search_state")
        # pending 草稿存在时优先给「仍在发布」提示（与 legacy _handle_reset_search 对齐）
        if had_pending:
            kind = (
                "简历"
                if session.pending_upload_intent == "upload_resume"
                else "岗位"
            )
            from app.services.upload_service import _FIELD_DISPLAY_NAMES
            field_name = _FIELD_DISPLAY_NAMES.get(
                session.awaiting_field, session.awaiting_field or "字段",
            )
            return [_reply(
                userid,
                command_service.RESET_SEARCH_PENDING_FMT.format(
                    kind=kind, field_name=field_name,
                ),
            )]
        if not had_search:
            return [_reply(userid, command_service.RESET_SEARCH_EMPTY)]
        return [_reply(userid, command_service.RESET_SEARCH_SUCCESS)]

    # 兜底（理论上不该走到这里）
    logger.warning(
        "_route_v2_cancel_reset: unexpected dialogue_act=%s", act,
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
    msg: WeComMessage | None = None,
) -> list[ReplyMessage]:
    data = intent_result.structured_data or {}
    cmd = data.get("command", "")
    args = data.get("args", "") or ""
    return command_service.execute(
        cmd, args, user_ctx, session, db,
        source_msg_id=msg.msg_id if msg is not None else None,
    )


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
        source_msg_id=msg.msg_id,
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
        source_msg_id=msg.msg_id,
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

    run_search_outcome = _run_search(
        None, criteria, msg.content or "", user_ctx, session, db,
        user_msg_id=msg.msg_id,
    )
    if run_search_outcome is None:
        # 搜索 handler 抛错；保持入库成功语义，active_flow 已在 _run_search 回到 idle
        return replies
    # Phase 5 §5.2：_run_search 现返回 (SearchResult, SearchOutcome)
    search_result, outcome = run_search_outcome

    # spec §9.2.1：0 命中也要追加“暂未找到”，并保持 active_flow=idle；
    # 有结果时 _run_search 已将 active_flow 推进到 search_active。
    search_replies = _post_search_dispatch(
        msg=msg,
        user_ctx=user_ctx,
        session=session,
        db=db,
        search_result=search_result,
        search_outcome=outcome,
        legacy_intent="upload_and_search",
        turn_asserted_slots={},
    )
    replies.extend(search_replies)
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
    # Routing, session direction and persisted reply intent must share the same
    # authoritative value. A legacy/fallback provider can label a worker turn as
    # search_worker even though the role constraint correctly executes job search.
    requested_intent = intent_result.intent
    # A broker's explicit direction switch is authoritative for subsequent
    # bare searches. Providers can label terse utterances as the opposite
    # direction; without an explicit object switch, continue the active mode.
    if (
        user_ctx.role == "broker"
        and requested_intent in {"search_job", "search_worker"}
        and session.broker_direction in {"search_job", "search_worker"}
        and requested_intent != session.broker_direction
    ):
        from app.services.dialogue_reducer import broker_explicit_direction

        if broker_explicit_direction(msg.content or "") is None:
            requested_intent = session.broker_direction
    effective_intent = _resolve_search_direction(
        requested_intent, user_ctx, session,
    )
    if effective_intent != intent_result.intent:
        intent_result = intent_result.model_copy(
            update={"intent": effective_intent, "missing_fields": []},
        )
    # 首次搜索：把 LLM 抽到的 structured_data 累积到 session.search_criteria
    # 即使本轮因为缺字段追问返回，也要保留部分条件，下一轮 follow_up 才有据可依
    new_criteria = dict(intent_result.structured_data or {})
    # Explicit dictionary-backed anchors are more reliable than a stochastic
    # provider label. They only override city/trade values literally present in
    # this search utterance; all other semantic extraction remains model-driven.
    new_criteria.update(
        intent_service.extract_explicit_search_anchors(msg.content or ""),
    )
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
    frame = _search_frame_for_intent(effective_intent)
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
            intent=effective_intent,
            criteria_snapshot=_snapshot_meta(session),
        )]

    # missing 为空：清搜索 awaiting，进入实际检索
    conversation_service.clear_search_awaiting(session)

    # Stage B P1-1：不能在默认合并前用 session.search_criteria 是否为空短路；
    # 否则 worker "看看新岗位" 这类空 structured_data 场景永远进不到
    # _apply_default_criteria，简历 expected_* 默认条件无机会兜底。
    criteria = dict(session.search_criteria)
    _start_recommendation_clock()
    run_search_outcome = _run_search(
        effective_intent, criteria, msg.content or "", user_ctx, session, db,
        user_msg_id=msg.msg_id,
    )
    if run_search_outcome is None:
        return [_reply(msg.from_user, SYSTEM_BUSY_REPLY)]
    # Phase 5 §5.2：_run_search 返回 (SearchResult, SearchOutcome)
    search_result, outcome = run_search_outcome
    # Phase 5 §5.2：_handle_search 也接 post_search_dispatch 三模式分流。
    return _post_search_dispatch(
        msg=msg,
        user_ctx=user_ctx,
        session=session,
        db=db,
        search_result=search_result,
        search_outcome=outcome,
        legacy_intent=effective_intent,
        turn_asserted_slots=new_criteria,
    )


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
    criteria_before_turn = dict(session.search_criteria or {})
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
        # Phase 3: consume the closed criteria patch contract explicitly.  The
        # resulting full snapshot is then applied through the existing session
        # helper so snapshot invalidation/version semantics stay centralized.
        from app.listing.search import apply_criteria_patch
        patched = apply_criteria_patch(
            session.search_criteria, intent_result.criteria_patch or [],
        )
        conversation_service.replace_criteria(session, patched)

    # Phase 1：消费 awaiting 字段（无论从 LLM 还是裸值兜底来）
    accepted_keys: list[str] = []
    if awaiting_consumed:
        accepted_keys.extend(awaiting_consumed)
    for k in (full_criteria or {}).keys():
        if k in (session.awaiting_fields or []):
            accepted_keys.append(k)
    if accepted_keys:
        conversation_service.consume_search_awaiting(session, accepted_keys)

    # legacy follow_up 的 structured_data 是“应用本轮后的完整快照”，不能把所有
    # 历史字段都当成本轮刚声明。按 before/after diff 生成 provider-independent delta，
    # 让 post-search 在 legacy/v2/fallback 三条路径上都能保护本轮新条件。
    turn_asserted_slots = {
        key: value
        for key, value in (session.search_criteria or {}).items()
        if criteria_before_turn.get(key) != value
    }

    # Stage B P1-1：同 _handle_search，不在默认合并前因 search_criteria 为空短路。
    # _run_search 会跑 _apply_default_criteria（含 worker 简历兜底），再交给
    # search_service.has_effective_search_criteria 决定是否真正查询。
    # 重新做一次检索：
    # - digest 变化：search_service 会按新 criteria 生成新快照
    # - digest 未变：相当于"再搜一次"，快照会被同样 digest 重置，对用户无感
    # - follow_up 没有显式方向，沿用 session.broker_direction（首次 search 时已写）
    criteria = dict(session.search_criteria)
    _start_recommendation_clock()
    run_search_outcome = _run_search(
        None, criteria, msg.content or "", user_ctx, session, db,
        user_msg_id=msg.msg_id,
    )
    if run_search_outcome is None:
        return [_reply(msg.from_user, SYSTEM_BUSY_REPLY)]
    # Phase 5 §5.2：_run_search 返回 (SearchResult, SearchOutcome)
    search_result, outcome = run_search_outcome
    # Phase 5 §5.2：_handle_follow_up 也接 post_search_dispatch 三模式分流。
    return _post_search_dispatch(
        msg=msg,
        user_ctx=user_ctx,
        session=session,
        db=db,
        search_result=search_result,
        search_outcome=outcome,
        legacy_intent="follow_up",
        turn_asserted_slots=turn_asserted_slots,
    )


def _handle_show_more(
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
    *,
    action_context=None,
) -> list[ReplyMessage]:
    # Phase 5 §5.0：show_more 现返回 tuple[SearchResult, SearchOutcome]
    direction = search_service.resolve_show_more_direction(session, user_ctx)
    permission_decision = check_search_permission(
        user_ctx,
        direction,
        entrypoint="message_router.show_more",
        request_id=msg.msg_id,
    )
    if not permission_decision.allowed:
        denied_result, _ = denied_search_response(permission_decision)
        return [_reply(msg.from_user, denied_result.reply_text)]
    experience_flags = _experience_flags_for(
        user_ctx, direction=direction, emit_log=True,
    )
    _start_recommendation_clock()
    if direction == "search_job" and _job_search_facade_enabled(user_ctx):
        try:
            from app.listing.search import JobSearchFacade, SearchTurn
            facade = JobSearchFacade(db, enabled=True)
            facade_response = facade.show_more(
                user_ctx, session,
                SearchTurn(
                    raw_query=msg.content or "", user_msg_id=msg.msg_id,
                    snapshot_id=getattr(session.candidate_snapshot, "snapshot_id", None),
                    page_number=len(session.shown_items) // 3 + 1,
                    page_size=3,
                ),
                db=db, experience_flags=experience_flags,
            )
            result, outcome = facade_response.result, facade_response.outcome
            if facade_response.used_facade:
                _render_facade_cards(result, facade_response.cards)
        except Exception as exc:
            # Snapshot paging itself already completed through the legacy
            # service; a projection failure must not lose that response.
            log_event("facade_fallback", direction="search_job", action="show_more", reason=type(exc).__name__)
            result, outcome = search_service.show_more(
                session, user_ctx, db, experience_flags=experience_flags,
            )
    else:
        if direction == "search_job":
            log_event(
                "facade_fallback", direction="search_job", action="show_more",
                reason=_job_search_facade_fallback_reason(user_ctx),
                user_msg_id=msg.msg_id,
            )
        result, outcome = search_service.show_more(
            session, user_ctx, db, experience_flags=experience_flags,
        )
    # Stage C1：show_more 后若快照仍存活则保持 search_active；否则降为 idle
    if session.candidate_snapshot is not None:
        session.active_flow = "search_active"
    else:
        session.active_flow = "idle"

    # Phase 5 §5.1：post_search_policy_mode 三模式分流。
    # - off：直接用 result.reply_text，逐字节等价 5.0；
    # - shadow：调 reducer 但只写日志，不影响 reply；
    # - on：构造 PostSearchContext 调 applier 拿最终 reply。
    # _handle_search / _handle_follow_up 暂不接通（5.1 reducer 在主搜索路径
    # 永远输出 no_action，行为等价 off；5.2 接通后会触发 auto_relax_and_retry
    # 等 action，那时再扩）。
    return _post_search_dispatch(
        msg=msg,
        user_ctx=user_ctx,
        session=session,
        db=db,
        search_result=result,
        search_outcome=outcome,
        legacy_intent="show_more",
    )


def _post_search_dispatch(
    *,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
    search_result,  # SearchResult
    search_outcome,  # SearchOutcome
    legacy_intent: str,
    turn_asserted_slots: dict | None = None,
) -> list[ReplyMessage]:
    """Phase 5 §5.1：post_search_policy_mode 三模式分流入口。

    legacy_intent 用于 off / shadow 模式构造与 5.0 之前完全等价的 ReplyMessage
    （含 intent / criteria_snapshot 字段），on 模式由 applier 自行渲染。

    Phase 5 §5.2：从 _v2_turn_context 读取真实 parse_result / decision（如有），
    让 reducer 准确判断"用户刚断言的字段是否被 relax step 覆盖"。

    Phase 5 §5.4：当 mode=on 时，进一步用 phase5_rollout_percentage hash 桶
    判定灰度命中；未命中桶则等价 off 行为（保 reply 与 5.0 前一致），让
    5%/25%/50%/100% 灰度阶梯真正生效。
    """
    mode = _settings_module.dialogue_policy.post_search_policy_mode
    # §9.5 P1-25：search_service 自己走完 fallback 步得到的结果是"系统自动放宽"，
    # 用户从未确认过，必须记 auto_relaxed；confirmed_relaxed 只属于
    # _route_v2_relaxation_response 里用户回"好的"之后的那次新请求。
    recommendation_fields = _recommendation_reply_fields(
        search_result,
        user_ctx.external_userid,
        msg.msg_id,
        request_kind=(
            "show_more" if legacy_intent == "show_more"
            else "auto_relaxed"
            if getattr(search_outcome, "applied_relax_step", None)
            else "initial_search"
        ),
        search_outcome=search_outcome,
    )

    if mode == "off":
        return [_reply(
            msg.from_user,
            search_result.reply_text,
            intent=legacy_intent,
            criteria_snapshot=_snapshot_meta(session),
            **_fields_for_body(
                recommendation_fields, search_result.reply_text, search_result,
            ),
        )]

    # Phase 5 §5.4：on 模式下进一步用 phase5_rollout_percentage hash 桶判定。
    # percentage>=100 全量命中；<=0 一律不命中（等价 off）；中间值按 userid hash。
    # shadow 不受 hash 桶约束（shadow 本来就只写日志不影响 reply，全量观测更有价值）。
    if mode == "on":
        from app.services.intent_service import is_phase5_policy_enabled
        if not is_phase5_policy_enabled(user_ctx.external_userid or ""):
            # 未命中灰度桶 → 等价 off 行为
            return [_reply(
                msg.from_user,
                search_result.reply_text,
                intent=legacy_intent,
                criteria_snapshot=_snapshot_meta(session),
                **_fields_for_body(
                    recommendation_fields, search_result.reply_text, search_result,
                ),
            )]

    # shadow / on 都需要构造 ctx + 调 reducer
    from app.services.dialogue_reducer import DialogueDecision
    from app.llm.base import DialogueParseResult
    from app.services.post_search_applier import apply_post_search_decision
    from app.services.post_search_reducer import (
        PostSearchContext,
        post_search_reduce,
    )

    # Phase 5 §5.2：优先从 turn-scoped holder 读 v2 真实 parse_result / decision
    # （由 _handle_text v2 路径在 dispatch 前暂存）；off / legacy 路径下没有，
    # 用高 confidence 的 stub 避免误触发 _decide_zero_result 的低置信度分支。
    # 第 8 轮 review fix 2：改用 ContextVar 而非 module-level dict。
    real_parse = _v2_parse_result.get()
    real_decision = _v2_decision.get()
    if real_parse is not None:
        parse_stub = real_parse
    else:
        parse_stub = DialogueParseResult(
            dialogue_act="chitchat",
            frame_hint="none",
            slots_delta={},
            merge_hint={},
            needs_clarification=False,
            confidence=1.0,  # 高 confidence 跳过 _decide_zero_result 低置信度分支
        )
    if real_decision is not None:
        decision_stub = real_decision
    else:
        decision_stub = DialogueDecision(
            dialogue_act="chitchat",
            resolved_frame="none",
            route_intent=legacy_intent,
            accepted_slots_delta=dict(turn_asserted_slots or {}),
        )

    experience_flags = _experience_flags_for(
        user_ctx, direction=search_outcome.direction, emit_log=False,
    )
    ps_decision = post_search_reduce(
        parse_result=parse_stub,
        decision=decision_stub,
        session=session,
        search_outcome=search_outcome,
        role=user_ctx.role,
        experience_flags=experience_flags,
    )

    if mode == "shadow":
        # 5.1 验收 #2：shadow 只写日志，不影响 reply。
        # Phase 5 §5.4：补 soft_pref_hits / applied_relax_step / final_count
        # 监控面板字段。
        _log_post_search_decision(
            mode="shadow",
            user_ctx=user_ctx,
            ps_decision=ps_decision,
            search_outcome=search_outcome,
        )
        return [_reply(
            msg.from_user,
            search_result.reply_text,
            intent=legacy_intent,
            criteria_snapshot=_snapshot_meta(session),
            **_fields_for_body(
                recommendation_fields, search_result.reply_text, search_result,
            ),
        )]

    # mode == "on"
    # Phase 5 §5.4：on 模式监控字段同步扩展
    _log_post_search_decision(
        mode="on",
        user_ctx=user_ctx,
        ps_decision=ps_decision,
        search_outcome=search_outcome,
    )
    ctx = PostSearchContext(
        decision=ps_decision,
        search_result=search_result,
        search_outcome=search_outcome,
        parse_result=parse_stub,
        dialogue_decision=decision_stub,
        session=session,
        msg=msg,
        user_ctx=user_ctx,
        db=db,
        raw_query=msg.content or "",
        role=user_ctx.role,
        experience_flags=experience_flags,
        recursion_depth=0,
    )
    replies = apply_post_search_decision(ctx)
    # applier 返回的 ReplyMessage 没有 intent / criteria_snapshot，由本函数补齐
    # 以保持与旧路径同构（便于 worker 落库 / 监控大盘）。
    #
    # P1-10：推荐上下文不能无条件补到每一条 recommendation_context 为空的回复上。
    # ask_clarification / paginate_no_more / suggest_relaxation 不含任何候选，
    # 挂上 delivery 就会派生出凭空的曝光事实（§10.1 行 2210-2211）。请求事实仍要写
    # 一条（§7.5），但只挂在真正渲染了候选的那条回复上；都没渲染时挂在第一条。
    return _attach_recommendation_fields(
        replies,
        recommendation_fields,
        search_result,
        intent=legacy_intent,
        criteria_snapshot=_snapshot_meta(session),
    )


def _attach_recommendation_fields(
    replies: list[ReplyMessage],
    recommendation_fields: dict,
    search_result,
    *,
    intent: str | None = None,
    criteria_snapshot: dict | None = None,
) -> list[ReplyMessage]:
    """把请求事实/delivery 挂到正确的那一条回复上（§7.5 + §10.1）。

    - 一次请求只写一条 request 事实，因此只挂一条回复；
    - ``delivery_id`` / ``recommendation_context`` 只挂给正文里真的渲染了候选的
      回复，其余回复只拿请求事实（或什么都不拿）。
    - applier 已经自己挂过（自动放宽二次检索）时不再覆盖。
    """
    already_attached = any(
        reply.recommendation_request is not None
        or reply.recommendation_context is not None
        for reply in replies
    )
    target_index = next(
        (
            index for index, reply in enumerate(replies)
            if _reply_renders_candidates(reply.content, search_result)
        ),
        0,
    )
    enriched: list[ReplyMessage] = []
    for index, reply in enumerate(replies):
        updates: dict = {}
        if not already_attached and index == target_index:
            updates.update(_fields_for_body(
                recommendation_fields, reply.content, search_result,
            ))
        if intent is not None and reply.intent is None:
            updates.update({
                "intent": intent,
                "criteria_snapshot": criteria_snapshot,
            })
        if updates:
            reply = reply.model_copy(update=updates)
        enriched.append(reply)
    return enriched


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

    session = conversation_service.load_session(userid)
    if session and _abandon_expired_pending_upload(session, db):
        upload_service.discard_unattached_media(db, msg.media_lifecycle_id)
        conversation_service.record_history(
            session, "assistant", PENDING_EXPIRED_REPLY,
        )
        conversation_service.save_session(userid, session)
        return [_reply(userid, PENDING_EXPIRED_REPLY)]
    if msg.expired_upload_draft:
        upload_service.discard_unattached_media(db, msg.media_lifecycle_id)
        return [_reply(userid, PENDING_EXPIRED_REPLY)]

    if not image_url:
        logger.warning("message_router: image msg without image_url, msg_id=%s", msg.msg_id)
        return [_reply(userid, IMAGE_DOWNLOAD_FAILED)]

    # 尝试挂载到当前上传流程。
    # 草稿存活时按 pending intent 挂载；草稿结束后只接受精确实体目标。
    # 禁止使用 current_intent 猜测“最近记录”，否则过期/取消/候选结束后
    # 排队图片可能误改旧岗位。
    if session and (
        session.pending_upload_intent
        or (
            session.attachment_target_type in {"job", "resume"}
            and type(session.attachment_target_id) is int
            and session.attachment_target_id > 0
        )
    ):
        feedback = upload_service.attach_image(
            external_userid=userid,
            image_key=image_url,
            media_lifecycle_id=msg.media_lifecycle_id,
            session=session,
            db=db,
        )
        conversation_service.save_session(userid, session)
        return [_reply(userid, feedback)]

    # 非上传流程：留存提示
    upload_service.discard_unattached_media(db, msg.media_lifecycle_id)
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


def _abandon_expired_pending_upload(session: SessionState, db: Session) -> bool:
    """Atomically apply the common expired-draft cleanup before routing."""
    if not upload_service.is_pending_upload_expired(session):
        return False
    upload_service.abandon_pending_upload(session, db)
    return True


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
    session: SessionState,
):
    """按优先级从三个来源抽取某字段的值（structured_data → criteria_patch → 规则）。"""
    # 1. structured_data
    sd, criteria_patch = _canonical_upload_patch_sources(session, intent_result)
    val = sd.get(field)
    if not _is_empty(val):
        return val

    # 2. criteria_patch
    for patch in criteria_patch:
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


def _canonical_upload_patch_sources(
    session: SessionState,
    intent_result: IntentResult,
) -> tuple[dict, list[dict]]:
    """按当前上传 frame 归一补字段别名，不改变搜索或岗位字段语义。"""
    structured_data = dict(intent_result.structured_data or {})
    criteria_patch = [dict(patch) for patch in intent_result.criteria_patch or []]
    if session.pending_upload_intent != "upload_resume":
        return structured_data, criteria_patch

    structured_data = slot_schema.remap_synonyms(
        "resume_upload", structured_data,
    )
    for patch in criteria_patch:
        field = patch.get("field")
        if not field:
            continue
        remapped = slot_schema.remap_synonyms(
            "resume_upload", {field: patch.get("value")},
        )
        patch["field"], patch["value"] = next(iter(remapped.items()))
    return structured_data, criteria_patch


def _merge_other_upload_fields(
    session: SessionState,
    intent_result: IntentResult,
) -> bool:
    """把 structured_data / criteria_patch 中除 awaiting_field 外的有效字段合入 pending。

    返回是否合入了任何新字段。这部分字段补全不视为“答非所问”。
    """
    merged_any = False
    sd, criteria_patch = _canonical_upload_patch_sources(session, intent_result)
    pending = dict(session.pending_upload or {})
    for k, v in sd.items():
        if _is_empty(v):
            continue
        if pending.get(k) != v:
            pending[k] = v
            merged_any = True
    for patch in criteria_patch:
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
        awaiting_value = _extract_field_value(
            awaiting, intent_result, raw_text, session,
        )

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

    # 真正的“答非所问”：累计 failed_patch_rounds。连续两次未识别时进入恢复提示，
    # 但保留草稿；只有显式取消、TTL 过期或成功入库才能清除用户已填写内容。
    session.failed_patch_rounds += 1
    if session.failed_patch_rounds >= 2:
        session.failed_patch_rounds = 0
        session.active_flow = "upload_collecting"
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
        source_msg_id=msg.msg_id,
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
    permission_decision = check_search_permission(
        user_ctx,
        direction,
        entrypoint="message_router.run_search",
        request_id=user_msg_id,
    )
    if not permission_decision.allowed:
        return denied_search_response(permission_decision)
    experience_flags = _experience_flags_for(
        user_ctx, direction=direction, emit_log=True,
    )
    composed = _apply_default_criteria(criteria, session, user_ctx, db, direction)
    # Phase 2: worker -> recruitment.job may use the structured facade.  The
    # facade delegates to this same legacy service and is strictly fail-open to
    # the legacy result when disabled, out of bucket, or structurally invalid.
    if direction == "search_job" and _job_search_facade_enabled(user_ctx):
        try:
            from app.listing.search import JobSearchFacade, SearchTurn

            facade_response = JobSearchFacade(db, enabled=True).search_jobs_v1(
                user_ctx, composed, session,
                SearchTurn(raw_query=raw_query, user_msg_id=user_msg_id), db=db,
                experience_flags=experience_flags,
            )
            result, outcome = facade_response.result, facade_response.outcome
            if facade_response.used_facade and facade_response.cards:
                _render_facade_cards(result, facade_response.cards)
                log_event("job_search_facade_served", facade_version=JobSearchFacade.version)
        except Exception as exc:
            log_event("facade_fallback", direction="search_job", reason=type(exc).__name__)
            result, outcome = search_service.search_jobs(
                composed, raw_query, session, user_ctx, db,
                user_msg_id=user_msg_id,
                experience_flags=experience_flags,
            )
    elif direction == "search_job":
        log_event(
            "facade_fallback", direction="search_job", action="search",
            reason=_job_search_facade_fallback_reason(user_ctx),
            user_msg_id=user_msg_id,
        )
        result, outcome = search_service.search_jobs(
            composed, raw_query, session, user_ctx, db,
            user_msg_id=user_msg_id,
            experience_flags=experience_flags,
        )
    else:
        result, outcome = search_service.search_workers(
            composed, raw_query, session, user_ctx, db,
            user_msg_id=user_msg_id,
            experience_flags=experience_flags,
        )

    # Phase 5 §5.2：available_relax_steps 现已由 search_jobs/search_workers 在
    # post_search_policy_mode=on 时内部填好（跳过 _run_*_fallback_steps 由 reducer
    # 接管）；off / shadow 模式下不需要 available_relax_steps（reducer 不接管）。
    # 本处不再重复 probe（避免无意义的 SQL 调用）。

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
    # Phase 5 §5.2：返回 (result, outcome)，让上游 _handle_search /
    # _handle_follow_up 接 post_search_reduce 三模式分流。
    return result, outcome


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
        from app.services.resume_mutation_service import online_resume_filters, utc_now_naive

        now = utc_now_naive()
        resume = db.query(Resume).filter(
            Resume.owner_userid == external_userid,
            *online_resume_filters(now=now),
        ).order_by(Resume.created_at.desc()).first()
    except Exception:
        logger.exception(
            "message_router: load worker resume defaults failed user_hash=%s",
            userid_hash(external_userid),
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
        if user_ctx.role == "factory":
            return ResolvedSearchDirection(
                "search_job",
                supported=False,
                reason_code="role_direction_forbidden",
            )
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


def _history_reply_content(reply: ReplyMessage) -> str:
    """Never persist recommendation plaintext in Redis or durable session payloads."""
    return (
        "[recommendation_delivery]"
        if reply.recommendation_context
        else reply.content
    )


def _record_reply_history(
    session: SessionState,
    reply: ReplyMessage,
) -> None:
    """Persist a redacted reply plus the non-secret delivery lookup marker."""
    delivery_id = reply.delivery_id
    if delivery_id is None and reply.recommendation_context is not None:
        delivery_id = reply.recommendation_context.delivery_id
    conversation_service.record_history(
        session,
        "assistant",
        _history_reply_content(reply),
        delivery_id=delivery_id if reply.recommendation_context else None,
    )


#: 只有真的把候选写进正文的回复才创建 delivery（§10.1），其余字段是纯请求事实。
_DELIVERY_ONLY_FIELDS = ("delivery_id", "recommendation_context")


def _request_only_fields(fields: dict) -> dict:
    """去掉 delivery 相关字段，只保留请求事实（§10.1 不创建 delivery 的分支）。"""
    return {
        key: value for key, value in fields.items()
        if key not in _DELIVERY_ONLY_FIELDS
    }


def _fields_for_body(fields: dict, content: str | None, search_result) -> dict:
    """按这条回复正文是否真的渲染了候选，决定给不给 delivery 字段。"""
    if not fields:
        return {}
    if _reply_renders_candidates(content, search_result):
        return fields
    return _request_only_fields(fields)


def _reply_renders_candidates(content: str | None, search_result) -> bool:
    """本条回复的正文里是否真的渲染了候选。

    §10.1.1：上下文只包含本次真正写入回复的候选。``ask_clarification`` /
    ``paginate_no_more`` / ``suggest_relaxation`` 这类纯文本回复不含任何候选，
    给它们挂 delivery 会凭空派生出曝光事实，污染 CTR 分母和曝光均衡输入。
    """
    if not getattr(search_result, "recommendation_items", None):
        return False
    body = (getattr(search_result, "reply_text", "") or "").strip()
    return bool(body) and body in (content or "")


def _is_recommendation_search(search_result, search_outcome) -> bool:
    """本轮是否发生过一次真实的推荐搜索（含零结果）。

    §7.5：off/legacy 只关闭新排序，不关闭可观测性，因此零结果与 legacy 请求同样
    要写 request 事实。但"快照已过期/没有可继续查看的结果"这类根本没查过库的
    回复不是搜索，不能伪造事实去污染零结果率。
    """
    if getattr(search_result, "recommendation_items", None):
        return True
    if int(getattr(search_result, "result_count", 0) or 0) > 0:
        return True
    if getattr(search_outcome, "snapshot_exhausted", False):
        return True
    criteria = getattr(search_outcome, "criteria_used", None) or {}
    return search_service.has_effective_search_criteria(criteria)


def _recommendation_reply_fields(
    search_result,
    userid: str,
    source_inbound_msg_id: str,
    request_kind: str = "initial_search",
    parent_request_id: str | None = None,
    *,
    search_outcome=None,
    attempt_kind: str | None = None,
    request_index: int | None = None,
    total_latency_ms: int | None = None,
    prior_search_result=None,
    prior_search_outcome=None,
) -> dict:
    """Carry the recommendation contract from search to the durable outbox.

    两件事必须分开：
    - **request 事实**：只要发生过一次真实推荐搜索就要产生，包括零结果和
      legacy/off/shadow（§7.5 行 1498-1500）。legacy 情形下 assignment 与
      algorithm_version 固定 ``legacy``、strategy_version_id 为 None。
    - **delivery**（加密正文 + 曝光派生）：只有本次回复真的把候选写进正文时才建
      （§10.1 行 2210-2211），所以 ``delivery_id`` / ``recommendation_context``
      只在 items 非空时返回，并由调用方挂到真正渲染候选的那条回复上。
    """
    if not _is_recommendation_search(search_result, search_outcome):
        return {}
    items = list(getattr(search_result, "recommendation_items", []) or [])
    assignment = getattr(search_result, "strategy_assignment", None)
    direction = (
        getattr(assignment, "direction", None)
        or getattr(search_outcome, "direction", None)
    )
    if direction not in {"search_job", "search_worker"}:
        return {}
    viewer_userid = str(userid or "")
    if not viewer_userid:
        # 没有 viewer 就无法归因，写进去只会污染请求级指标。
        logger.warning(
            "recommendation request fact skipped: empty viewer_userid msg_id=%s",
            source_inbound_msg_id,
        )
        return {}

    served_assignment = getattr(assignment, "assignment", "legacy") or "legacy"
    is_legacy = served_assignment == "legacy"
    algorithm_version = (
        "legacy" if is_legacy
        else (getattr(assignment, "algorithm_version", None) or "recommendation-v1")
    )
    strategy_version_id = (
        None if is_legacy else getattr(assignment, "strategy_version_id", None)
    )
    query_digest = str(getattr(search_result, "query_digest", "") or "")
    snapshot_id = getattr(search_result, "snapshot_id", None)
    result_request_id = getattr(search_result, "request_id", None)
    if request_kind in {"show_more", "confirmed_relaxed"}:
        # §9.4：查看更多和"用户确认放宽"各自是新的 request，parent 指向原 request。
        request_id = str(uuid.uuid4())
        parent_request_id = parent_request_id or result_request_id
    elif request_kind == "auto_relaxed":
        # 自动放宽的最终检索可能重新走了一次 assignment/shadow 提交；使用最终
        # SearchResult 的 request_id，前一轮查询作为同 request 的 additional attempt。
        request_id = result_request_id or parent_request_id or str(uuid.uuid4())
        parent_request_id = None
    else:
        # §9.4 行 1839-1840：初次搜索后自动放宽仍然只有一条 request，沿用初次搜索的
        # request_id（后续放宽只是它的第二个 attempt），因此不能再挂 parent。
        request_id = parent_request_id or result_request_id or str(uuid.uuid4())
        parent_request_id = None

    # §9.4 请求级聚合：owner 集中度与探索位从实际服务的 items 直接算。
    owner_counts: dict[str, int] = {}
    for item in items:
        owner = getattr(item, "owner_userid", None)
        if owner:
            owner_counts[owner] = owner_counts.get(owner, 0) + 1
    candidate_ids = [str(cid) for cid in (
        getattr(search_result, "candidate_ids", None) or []
    )][:50]
    # 兼容旧调用方：新搜索路径会为 legacy 结果生成仅用于事实记录的
    # RecommendationItem；尚未迁移的调用方仍可用 result_count 表示服务数量。
    result_count = len(items) or int(getattr(search_result, "result_count", 0) or 0)
    exhausted = bool(getattr(search_outcome, "snapshot_exhausted", False))
    current_probe_records = [
        dict(probe)
        for probe in (getattr(search_outcome, "relax_probe_results", None) or [])
        if isinstance(probe, dict)
    ]
    probe_steps = [
        str(probe.get("step"))
        for probe in current_probe_records
        if probe.get("step") and probe.get("step") != "initial"
    ]

    def _attempt_from_result(result, *, kind: str) -> dict:
        ids = [
            str(value) for value in (
                getattr(result, "candidate_ids", None) or []
            )
        ][:50]
        return {
            "step": kind,
            "attempt_kind": kind,
            "criteria_digest": str(
                getattr(result, "query_digest", "") or ""
            ),
            "candidate_count": len(ids),
            "candidate_ids": ids,
            "precision_pool_ids": [
                str(value) for value in (
                    getattr(result, "precision_pool_ids", None) or []
                )
            ][:50],
            "result_count": int(getattr(result, "result_count", 0) or 0),
            "is_zero_result": not ids,
            "scoring_time_utc": getattr(result, "scoring_time_utc", None),
            "llm_status": getattr(result, "llm_status", "skipped"),
            "llm_input_tokens": getattr(result, "llm_input_tokens", None),
            "llm_output_tokens": getattr(result, "llm_output_tokens", None),
            "llm_retry_count": max(
                0, int(getattr(result, "llm_retry_count", 0) or 0),
            ),
            "ranking_fallback": getattr(result, "ranking_fallback", None),
            "ranking_latency_ms": max(
                0, int(getattr(result, "ranking_latency_ms", 0) or 0),
            ),
        }

    additional_attempts: list[dict] = []
    if prior_search_result is not None:
        additional_attempts.append(_attempt_from_result(
            prior_search_result, kind="initial",
        ))
        additional_attempts.extend(
            dict(probe)
            for probe in (
                getattr(prior_search_outcome, "relax_probe_results", None) or []
            )
            if isinstance(probe, dict)
        )
    additional_attempts.extend(current_probe_records)
    probe_steps = list(dict.fromkeys(
        probe_steps + [
            str(attempt.get("step"))
            for attempt in additional_attempts
            if attempt.get("step") and attempt.get("step") != "initial"
        ],
    ))
    if request_kind == "auto_relaxed":
        for index, attempt in enumerate(additional_attempts):
            attempt["attempt_no"] = index
            if attempt.get("step") != "initial":
                attempt["attempt_kind"] = "relax_probe"
        served_attempt_no = len(additional_attempts)
    else:
        for index, attempt in enumerate(additional_attempts, start=1):
            attempt["attempt_no"] = index
            attempt["attempt_kind"] = "relax_probe"
        served_attempt_no = 0

    fact = RecommendationRequestFact(
        request_id=request_id,
        source_inbound_msg_id=source_inbound_msg_id,
        request_index=request_index,
        request_kind=request_kind,
        parent_request_id=parent_request_id,
        viewer_userid=viewer_userid,
        direction=direction,
        query_digest=query_digest,
        execution_mode=getattr(assignment, "execution_mode", None) or "off",
        served_assignment=served_assignment,
        served_strategy_version_id=strategy_version_id,
        candidate_strategy_version_id=getattr(
            assignment, "candidate_version_id", None,
        ),
        algorithm_version=algorithm_version,
        snapshot_id=snapshot_id,
        candidate_count=len(candidate_ids),
        candidate_ids=candidate_ids,
        precision_pool_ids=[str(pid) for pid in (
            getattr(search_result, "precision_pool_ids", None) or []
        )][:50],
        served_top_ids=[str(item.target_id) for item in items],
        served_owner_count=len(owner_counts),
        served_max_owner_items=max(owner_counts.values(), default=0),
        served_exploration_count=sum(
            1 for item in items if getattr(item, "is_exploration", False)
        ),
        result_count=result_count,
        # §9.4：show_more 的零结果固定 false，耗尽走 show_more_exhausted。
        is_zero_result=(False if request_kind == "show_more" else result_count == 0),
        show_more_exhausted=(exhausted if request_kind == "show_more" else False),
        attempt_kind=(
            attempt_kind
            or ATTEMPT_KIND_BY_REQUEST_KIND.get(request_kind, "initial")
        ),
        relax_probe_steps=probe_steps,
        additional_attempts=additional_attempts,
        attempt_no=served_attempt_no,
        scoring_time_utc=getattr(search_result, "scoring_time_utc", None),
        llm_status=getattr(search_result, "llm_status", "skipped"),
        llm_input_tokens=getattr(search_result, "llm_input_tokens", None),
        llm_output_tokens=getattr(search_result, "llm_output_tokens", None),
        llm_retry_count=max(
            0, int(getattr(search_result, "llm_retry_count", 0) or 0),
        ),
        ranking_fallback=getattr(search_result, "ranking_fallback", None),
        ranking_latency_ms=max(
            0, int(getattr(search_result, "ranking_latency_ms", 0) or 0),
        ),
        attempt_latency_ms=max(
            0, int(getattr(search_result, "ranking_latency_ms", 0) or 0),
        ),
        total_latency_ms=(
            _recommendation_elapsed_ms() if total_latency_ms is None
            else max(0, int(total_latency_ms))
        ),
    )

    fields: dict = {"recommendation_request": fact}
    if assignment is not None:
        fields["strategy_assignment"] = assignment
    if not items:
        # 零结果：只有请求事实，不建 delivery，也就不会派生虚假曝光。
        return fields
    context = RecommendationDeliveryContext(
        delivery_id=str(uuid.uuid4()),
        request_id=request_id,
        snapshot_id=snapshot_id,
        viewer_userid=viewer_userid,
        direction=direction,
        assignment=served_assignment,
        strategy_version_id=strategy_version_id,
        algorithm_version=algorithm_version,
        query_digest=query_digest,
        items=items,
    )
    fields["delivery_id"] = context.delivery_id
    fields["recommendation_context"] = context
    return fields


def _reply(
    userid: str,
    content: str,
    intent: str | None = None,
    criteria_snapshot: dict | None = None,
    **recommendation_fields,
) -> ReplyMessage:
    """构造 ReplyMessage；intent / criteria_snapshot 将被 Worker 落 conversation_log。"""
    return ReplyMessage(
        userid=userid,
        content=content,
        intent=intent,
        criteria_snapshot=criteria_snapshot,
        **recommendation_fields,
    )
