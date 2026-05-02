"""上传服务（Phase 3）。

岗位/简历入库编排：消费 IntentResult → 必填字段检查 → 审核 → 入库。
不重复调用 LLM；追问轮数由 session.follow_up_rounds 统一承载。
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.llm.base import IntentResult
from app.llm.prompts import JOB_REQUIRED_FIELDS, RESUME_REQUIRED_FIELDS
from app.models import Job, Resume, SystemConfig
from app.services import audit_service, conversation_service
from app.services.user_service import UserContext
from app.schemas.conversation import SessionState

# 单条记录最多挂载图片数（与 system_config.upload.max_images 对齐）
_MAX_IMAGES_PER_RECORD = 5

logger = logging.getLogger(__name__)

# 必填字段的中文展示名（用于生成追问文案）
_FIELD_DISPLAY_NAMES = {
    "city": "工作城市",
    "job_category": "工种",
    "salary_floor_monthly": "月薪下限",
    "pay_type": "计薪方式",
    "headcount": "招聘人数",
    "expected_cities": "期望城市",
    "expected_job_categories": "期望工种",
    "salary_expect_floor_monthly": "期望月薪",
    "gender": "性别",
    "age": "年龄",
}

MAX_FOLLOW_UP_ROUNDS = 2

# Stage A：上传草稿默认 10 分钟过期（详见 docs/multi-turn-upload-stage-a-implementation.md §3.4）
PENDING_UPLOAD_TTL_MINUTES = 10


def _save_pending_upload(
    session: SessionState,
    intent: str,
    structured_data: dict,
    missing: list[str],
    raw_text: str,
) -> None:
    """把当前轮抽取到的字段合并进 session 草稿，并刷新过期时间。

    raw_text 为当前轮用户原文（非已拼接结果）。函数会去重追加到
    pending_raw_text_parts；与最后一条相同则跳过，避免重复合并。
    """
    now = datetime.now(timezone.utc)
    # 固定窗口：expires_at = created_at + 10 分钟，subsequent 轮次只更新 updated_at。
    # 避免用户用 chitchat 间歇性"续命"陈旧草稿（spec §3.4 / §9.4）。
    if not session.pending_started_at:
        session.pending_started_at = now.isoformat()
        session.pending_expires_at = (
            now + timedelta(minutes=PENDING_UPLOAD_TTL_MINUTES)
        ).isoformat()
    session.pending_updated_at = now.isoformat()

    # 合并结构化字段（新值覆盖旧值）。
    if structured_data:
        merged = dict(session.pending_upload or {})
        for k, v in structured_data.items():
            if v is None:
                continue
            if isinstance(v, (list, str)) and len(v) == 0:
                continue
            merged[k] = v
        session.pending_upload = merged

    session.pending_upload_intent = intent
    session.awaiting_field = missing[0] if missing else None

    # 保留每一轮的用户原文（仅用户原始消息，不混入系统话术）。
    if raw_text:
        text = raw_text.strip()
        if text and (not session.pending_raw_text_parts
                     or session.pending_raw_text_parts[-1] != text):
            session.pending_raw_text_parts.append(text)

    # Stage C1（spec §9.1）：草稿创建/续写期间 active_flow 钉在 upload_collecting。
    session.active_flow = "upload_collecting"


def _build_final_raw_text(session: SessionState, current_raw_text: str) -> str:
    """构建入库时的 raw_text：parts + 当前轮去重拼接。"""
    parts = list(session.pending_raw_text_parts or [])
    current = (current_raw_text or "").strip()
    if current and (not parts or parts[-1] != current):
        parts.append(current)
    return "\n".join(parts) if parts else (current_raw_text or "")


def clear_pending_upload(session: SessionState) -> None:
    """清空 Stage A 上传草稿过渡字段。

    Stage C1：同时复位 failed_patch_rounds、conflict_followup_rounds 与 active_flow。
    调用方若希望保留 active_flow（例如随后转入 search_active），可在 clear 之后再行覆写。
    """
    session.pending_upload = {}
    session.pending_upload_intent = None
    session.awaiting_field = None
    session.pending_started_at = None
    session.pending_updated_at = None
    session.pending_expires_at = None
    session.pending_raw_text_parts = []
    session.follow_up_rounds = 0
    session.failed_patch_rounds = 0
    session.conflict_followup_rounds = 0
    session.pending_interruption = None
    session.active_flow = "idle"


def is_pending_upload_expired(session: SessionState) -> bool:
    """判断 pending upload 是否已过期。无草稿视为未过期。

    防御点：
    1. 解析失败按过期处理（脏数据不能卡住流程）。
    2. 解析出 naive datetime 时补 UTC tzinfo，避免与 aware now 比较抛 TypeError。
       我们写入时全部用 datetime.now(timezone.utc).isoformat()，但旧数据 / 未来 bug
       可能产生 naive 字符串，比较前必须归一化。
    """
    if not session.pending_expires_at:
        return False
    try:
        expires = datetime.fromisoformat(session.pending_expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expires
    except (TypeError, ValueError):
        return True


@dataclass
class UploadResult:
    success: bool
    reply_text: str
    entity_type: str | None = None  # "job" / "resume"
    entity_id: int | None = None
    needs_followup: bool = False


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def process_upload(
    user_ctx: UserContext,
    intent_result: IntentResult,
    raw_text: str,
    image_keys: list[str],
    session: SessionState,
    db: Session,
) -> UploadResult:
    """上传编排主入口。"""
    entity_type = _resolve_entity_type(intent_result.intent, user_ctx.role)
    if entity_type is None:
        return UploadResult(
            success=False,
            reply_text="无法确定您要发布的内容类型，请重新描述。",
        )

    required = JOB_REQUIRED_FIELDS if entity_type == "job" else RESUME_REQUIRED_FIELDS
    data = intent_result.structured_data

    # 检查缺失必填字段
    missing = _check_required_fields(data, required)

    if missing:
        # Stage C1（spec §2.6）：max rounds 主退出由 message_router._handle_field_patch 的
        # failed_patch_rounds 全权管控；process_upload 不再用 follow_up_rounds 早退，
        # 否则用户分多轮成功补不同有效字段时（"5500月薪" → "包吃住" → ...）会因为
        # follow_up_rounds 累计 ≥ MAX 被这里再次清掉，与 spec §9.5 "补了其它有效字段不算
        # failed" 的语义直接冲突。follow_up_rounds 仅作为 Stage A/B 兼容计数器保留。
        _save_pending_upload(
            session=session,
            intent=intent_result.intent,
            structured_data=data,
            missing=missing,
            raw_text=raw_text,
        )

        # 生成追问文本
        conversation_service.increment_follow_up(session)
        followup_text = _generate_followup_text(missing)
        return UploadResult(
            success=False,
            reply_text=followup_text,
            needs_followup=True,
        )

    # 必填字段齐全 → 先审核文本（不写 audit_log），再入库，最后用真实 ID 写 audit_log
    ttl_days = _read_ttl_days(entity_type, db)

    # Stage A：合并多轮原文（pending_raw_text_parts + 当前轮）作为最终 raw_text
    final_raw_text = _build_final_raw_text(session, raw_text)

    # 审核（此时还不写 audit_log，因为实体尚未入库）
    audit_result = audit_service.audit_content_only(
        text=final_raw_text,
        db=db,
    )

    # 入库（带审核结果）
    if entity_type == "job":
        entity = _create_job(
            data, user_ctx, audit_result, ttl_days, final_raw_text, image_keys, db,
        )
    else:
        entity = _create_resume(
            data, user_ctx, audit_result, ttl_days, final_raw_text, image_keys, db,
        )

    # 用真实实体 ID 写 audit_log
    audit_service.write_audit_log_for_result(
        entity_type, entity.id, audit_result, db,
    )

    # Stage A：入库成功后清 pending（含 follow_up_rounds 重置）
    clear_pending_upload(session)

    # 根据审核状态生成回复
    reply = _audit_status_reply(audit_result.status, entity_type)

    return UploadResult(
        success=True,
        reply_text=reply,
        entity_type=entity_type,
        entity_id=entity.id,
    )


def attach_image(
    external_userid: str,
    image_key: str,
    session: SessionState,
    db: Session,
) -> str:
    """将已保存的图片 key 附加到用户当前上传流程的实体上。

    Phase 4 新增：图片下载由 Worker 完成后，message_router 调用本方法
    把图片挂到最近一条未过期的岗位/简历上（由 session.current_intent 和用户
    角色共同决定实体类型）。不做 OCR，不参与字段抽取。

    Args:
        external_userid: 上传者 external_userid
        image_key: 存储层返回的 storage key 或 URL
        session: 当前会话（用于判断 current_intent）
        db: DB Session

    Returns:
        用户可读的反馈文案（成功 / 找不到实体 / 数量超限）
    """
    if not image_key:
        return "图片保存失败，请稍后重试。"

    # Stage C1（spec §2.10）：pending_upload_intent 优先 —
    # 草稿存活时（无论 active_flow 是 upload_collecting 还是 upload_conflict）
    # 都按 origin intent 决定实体类型；回落 current_intent 兼容旧 session（C2 删除回落）。
    if session.pending_upload_intent:
        entity_type = _attach_target_entity_type(session.pending_upload_intent)
    else:
        entity_type = _attach_target_entity_type(session.current_intent)

    model_cls = Job if entity_type == "job" else Resume
    now = datetime.now(timezone.utc)
    record = db.query(model_cls).filter(
        model_cls.owner_userid == external_userid,
        model_cls.deleted_at.is_(None),
        model_cls.expires_at > now,
    ).order_by(model_cls.created_at.desc()).first()

    if record is None:
        return "图片已收到，但未找到正在处理的上传记录；请先用文字发布岗位/简历，再补充图片。"

    # 追加到 images JSON 数组（去重 + 数量上限）
    images = list(record.images) if record.images else []
    if image_key in images:
        return "该图片已附加，无需重复发送。"
    if len(images) >= _MAX_IMAGES_PER_RECORD:
        return f"图片数量已达上限（{_MAX_IMAGES_PER_RECORD} 张），无法再添加。"

    images.append(image_key)
    record.images = images
    db.flush()

    kind = "岗位" if entity_type == "job" else "简历"
    logger.info(
        "upload_service.attach_image: userid=%s entity=%s id=%s images_count=%d",
        external_userid, entity_type, record.id, len(images),
    )
    return f"图片已附加到您最近一条{kind}信息（第 {len(images)} 张）。"


def _attach_target_entity_type(current_intent: str | None) -> str:
    """根据会话 current_intent 推断图片挂载目标。"""
    if current_intent in ("upload_job", "upload_and_search"):
        return "job"
    return "resume"


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _resolve_entity_type(intent: str, role: str) -> str | None:
    """根据意图和角色确定实体类型。"""
    if intent in ("upload_job", "upload_and_search"):
        return "job"
    if intent == "upload_resume":
        return "resume"
    # follow_up 场景需要从上下文推断
    if role == "worker":
        return "resume"
    if role in ("factory", "broker"):
        return "job"
    return None


def _check_required_fields(data: dict, required: frozenset) -> list[str]:
    """返回缺失的必填字段列表。"""
    missing = []
    for f in sorted(required):
        val = data.get(f)
        if val is None:
            missing.append(f)
        elif isinstance(val, (list, str)) and len(val) == 0:
            missing.append(f)
    return missing


def _generate_followup_text(missing: list[str], frame: str | None = None) -> str:
    """生成上传草稿追问文本。schema-driven（阶段三 P2）。

    schema 渲染失败时回退到原 inline + list 拼接，避免线上回复变空白。
    frame 缺省时按 missing 字段名兜底推断（含 expected_* 判定为 resume_upload，
    否则 job_upload）。
    """
    if not missing:
        return ""
    if frame is None:
        frame = _infer_upload_frame(missing)
    try:
        from app.dialogue import slot_schema as _ss
        text = _ss.render_missing_followup(
            missing, frame, context="upload",
            fallback_display=_FIELD_DISPLAY_NAMES,
        )
        if text:
            return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("upload_service: slot_schema render_missing_followup failed: %s", exc)
    # fallback：阶段一/二的拼接行为
    names = [_FIELD_DISPLAY_NAMES.get(f, f) for f in missing]
    if len(names) <= 2:
        return f"还需要您补充一下：{'和'.join(names)}，方便我帮您处理。"
    lines = "\n".join(f"- {n}" for n in names)
    return f"还缺少以下信息，请补充：\n{lines}"


def _infer_upload_frame(missing: list[str]) -> str | None:
    if not missing:
        return None
    for f in missing:
        if f.startswith("expected_") or f in {
            "salary_expect_floor_monthly", "education", "work_experience",
        }:
            return "resume_upload"
    return "job_upload"


def _read_ttl_days(entity_type: str, db: Session) -> int:
    """从 system_config 读取 TTL 天数。"""
    key = f"ttl.{entity_type}.days"
    config = db.query(SystemConfig).filter(
        SystemConfig.config_key == key,
    ).first()
    if config:
        try:
            return int(config.config_value)
        except (ValueError, TypeError):
            pass
    return 30  # 默认 30 天


def _extract_scalar(data: dict, key: str, default: str = "") -> str:
    """从 data 中提取标量值。如果值是 list，取第一个元素。"""
    val = data.get(key, default)
    if isinstance(val, list):
        return val[0] if val else default
    return val if val is not None else default


def _create_job(
    data: dict,
    user_ctx: UserContext,
    audit_result,
    ttl_days: int,
    raw_text: str,
    image_keys: list[str],
    db: Session,
) -> Job:
    """创建岗位记录。"""
    now = datetime.now(timezone.utc)
    job = Job(
        owner_userid=user_ctx.external_userid,
        city=_extract_scalar(data, "city", ""),
        job_category=data.get("job_category", ""),
        salary_floor_monthly=data.get("salary_floor_monthly", 0),
        pay_type=data.get("pay_type", "月薪"),
        headcount=data.get("headcount", 1),
        gender_required=data.get("gender_required", "不限"),
        is_long_term=data.get("is_long_term", True),
        raw_text=raw_text,
        description=data.get("description") or raw_text,
        images=image_keys or None,
        audit_status=audit_result.status,
        audit_reason=audit_result.reason or None,
        audited_by="system",
        audited_at=now,
        expires_at=now + timedelta(days=ttl_days),
        # 可选软匹配字段
        district=data.get("district"),
        salary_ceiling_monthly=data.get("salary_ceiling_monthly"),
        provide_meal=data.get("provide_meal"),
        provide_housing=data.get("provide_housing"),
        dorm_condition=data.get("dorm_condition"),
        shift_pattern=data.get("shift_pattern"),
        work_hours=data.get("work_hours"),
        accept_couple=data.get("accept_couple"),
        accept_student=data.get("accept_student"),
        accept_minority=data.get("accept_minority"),
        age_min=data.get("age_min"),
        age_max=data.get("age_max"),
        height_required=data.get("height_required"),
        experience_required=data.get("experience_required"),
        education_required=data.get("education_required"),
        rebate=data.get("rebate"),
        employment_type=data.get("employment_type"),
        contract_type=data.get("contract_type"),
        min_duration=data.get("min_duration"),
        job_sub_category=data.get("job_sub_category"),
    )
    db.add(job)
    db.flush()
    return job


def _create_resume(
    data: dict,
    user_ctx: UserContext,
    audit_result,
    ttl_days: int,
    raw_text: str,
    image_keys: list[str],
    db: Session,
) -> Resume:
    """创建简历记录。"""
    now = datetime.now(timezone.utc)
    resume = Resume(
        owner_userid=user_ctx.external_userid,
        expected_cities=data.get("expected_cities", []),
        expected_job_categories=data.get("expected_job_categories", []),
        salary_expect_floor_monthly=data.get("salary_expect_floor_monthly", 0),
        gender=data.get("gender", "男"),
        age=data.get("age", 0),
        accept_long_term=data.get("accept_long_term", True),
        accept_short_term=data.get("accept_short_term", False),
        raw_text=raw_text,
        description=data.get("description") or raw_text,
        images=image_keys or None,
        audit_status=audit_result.status,
        audit_reason=audit_result.reason or None,
        audited_by="system",
        audited_at=now,
        expires_at=now + timedelta(days=ttl_days),
        # 可选软匹配字段
        expected_districts=data.get("expected_districts"),
        height=data.get("height"),
        weight=data.get("weight"),
        education=data.get("education"),
        work_experience=data.get("work_experience"),
        accept_night_shift=data.get("accept_night_shift"),
        accept_standing_work=data.get("accept_standing_work"),
        accept_overtime=data.get("accept_overtime"),
        accept_outside_province=data.get("accept_outside_province"),
        couple_seeking_together=data.get("couple_seeking_together"),
        has_health_certificate=data.get("has_health_certificate"),
        ethnicity=data.get("ethnicity"),
        available_from=data.get("available_from"),
        has_tattoo=data.get("has_tattoo"),
        taboo=data.get("taboo"),
    )
    db.add(resume)
    db.flush()
    return resume


def _audit_status_reply(status: str, entity_type: str) -> str:
    """根据审核状态生成回复文案。"""
    type_name = "岗位信息" if entity_type == "job" else "简历信息"
    if status == "passed":
        return f"您的{type_name}已入库，将进入匹配池。"
    if status == "pending":
        return f"您的{type_name}已收到，正在等待人工审核，通过后即可进入匹配池。"
    if status == "rejected":
        return f"您的{type_name}已收到，但未通过内容审核。如有疑问请联系客服。"
    return f"您的{type_name}已收到。"
