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
            user_msg_id=msg.msg_id,
        )
    except Exception as exc:
        logger.exception("message_router: classify_intent failed: %s", exc)
        conversation_service.save_session(userid, session)
        return [_reply(userid, SYSTEM_BUSY_REPLY)]

    # ---- Stage A 上传草稿守卫 ----
    # 顺序：command → timeout → cancel → field patch → 正常 dispatch
    # 命令优先，但是否清 pending 由命令语义决定（阶段 A 沿用 command_service）。
    if intent_result.intent != "command" and _has_pending_upload(session):
        pending_reply = _handle_pending_upload(
            content, intent_result, msg, user_ctx, session, db,
        )
        if pending_reply is not None:
            if pending_reply:
                conversation_service.record_history(
                    session, "assistant", pending_reply[0].content,
                )
            conversation_service.save_session(userid, session)
            return pending_reply

    intent = intent_result.intent
    # Stage A：pending 存活时把 current_intent 钉在 pending_upload_intent 上，
    # 否则 /帮助 等命令会把它覆盖成 "command"，导致后续图片走非上传分支无法挂载。
    # 详见 docs/multi-turn-upload-stage-a-implementation.md §5 验收标准 7。
    if _has_pending_upload(session):
        session.current_intent = session.pending_upload_intent
    else:
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

    # upload_and_search 的方向：
    #   - 工人：search_job（找工作）— 用简历字段映射成 city/job_category
    #   - 厂家/中介：search_worker（找工人）— 直接用 city/job_category
    # _resolve_search_direction 按角色兜底即可（传 None）
    direction = _resolve_search_direction(None, user_ctx, session)
    criteria = _build_upload_and_search_criteria(
        intent_result.structured_data or {}, direction,
    )
    if criteria:
        session.search_criteria = {**session.search_criteria, **criteria}

    search_result = _run_search(
        None, criteria, msg.content or "", user_ctx, session, db,
        user_msg_id=msg.msg_id,
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

    # Bug 1 修复：合并后再判 missing。LLM 在短文本上会把已知字段错误地标进
    # missing_fields（如用户说"西安有吗"时，session 已有 job_category="餐饮"
    # 但 LLM 仍标 job_category 为 missing），需要按合并后的 session.search_criteria
    # 复核——已填字段从 missing 里剔除。详见 _compute_search_missing。
    missing = _compute_search_missing(intent_result, session)
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


def _handle_pending_upload(
    content: str,
    intent_result: IntentResult,
    msg: WeComMessage,
    user_ctx: UserContext,
    session: SessionState,
    db: Session,
) -> list[ReplyMessage] | None:
    """Stage A 上传草稿守卫：返回 None 表示放行到正常 dispatch。

    顺序：
      1. timeout 检查
      2. cancel 检查
      3. 字段补全（无字段则提示，不调用搜索）
    """
    userid = msg.from_user

    # 1. 过期检查
    if upload_service.is_pending_upload_expired(session):
        was_patch = _looks_like_upload_patch(content)
        upload_service.clear_pending_upload(session)
        if was_patch:
            return [_reply(userid, PENDING_EXPIRED_REPLY)]
        # 否则放行到正常 dispatch
        return None

    # 2. cancel 强规则
    if _is_cancel(content, intent_result):
        upload_service.clear_pending_upload(session)
        return [_reply(userid, PENDING_CANCELLED_REPLY)]

    # 3. 字段补全
    awaiting = session.awaiting_field
    raw_text = content or ""

    awaiting_value = None
    if awaiting:
        awaiting_value = _extract_field_value(awaiting, intent_result, raw_text)

    if awaiting and not _is_empty(awaiting_value):
        # 补到了 awaiting_field：merge 主字段
        pending = dict(session.pending_upload or {})
        pending[awaiting] = awaiting_value
        session.pending_upload = pending
        # 顺带合并本轮其它有效上传字段
        _merge_other_upload_fields(session, intent_result)
        return _commit_pending_or_followup(msg, user_ctx, session, db)

    # 没补到 awaiting_field：尝试合并其它有效上传字段
    other_merged = _merge_other_upload_fields(session, intent_result)
    if other_merged:
        return _commit_pending_or_followup(msg, user_ctx, session, db)

    # Stage B P2-1：chitchat 不消耗追问计数（spec §9.8）。
    # upload_collecting 中遇到 chitchat 应保留 pending、回闲聊文本 + 提醒未完成事项，
    # 不动 follow_up_rounds / failed_patch_rounds，避免 "你好" "谢谢" 把草稿挤掉。
    if intent_result.intent == "chitchat":
        field_name = _field_display_name(awaiting) if awaiting else "需要的字段"
        text = (
            f"{_chitchat_text(user_ctx)}\n\n"
            f"您当前还在发布岗位/简历，请补充{field_name}，或发送 /取消 放弃草稿。"
        )
        return [_reply(userid, text)]

    # 既没补 awaiting_field 也没补其它字段，且不是 chitchat：让 max rounds 计数前进，
    # 避免用户答非所问无限刷请求把陈旧草稿挂着。Stage A §3.4 “沿用 follow_up_rounds”。
    if session.follow_up_rounds >= upload_service.MAX_FOLLOW_UP_ROUNDS:
        upload_service.clear_pending_upload(session)
        return [_reply(userid, PENDING_MAX_ROUNDS_REPLY)]
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
        return search_service.search_jobs(
            composed, raw_query, session, user_ctx, db, user_msg_id=user_msg_id,
        )
    return search_service.search_workers(
        composed, raw_query, session, user_ctx, db, user_msg_id=user_msg_id,
    )


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
