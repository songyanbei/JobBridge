"""审核服务（Phase 3）。

敏感词检测 + LLM 安全检查接入点 + 风险等级聚合 + audit_log 写入。
"""
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import AuditLog, DictSensitiveWord

logger = logging.getLogger(__name__)


@dataclass
class AuditResult:
    """审核判定结果。"""
    status: str  # passed / pending / rejected
    reason: str  # 审核理由
    matched_words: list[dict] = field(default_factory=list)  # [{"word":..., "level":...}]


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def audit_content(
    text: str,
    entity_type: str,
    entity_id: str | int,
    db: Session,
    images: list[str] | None = None,
) -> AuditResult:
    """对上传内容执行审核（含 audit_log 写入）。

    适用于实体已入库、有真实 ID 的场景。
    """
    result = audit_content_only(text, db, images)

    # 写 audit_log（passed / rejected 写，pending 不写）
    write_audit_log_for_result(entity_type, entity_id, result, db)

    return result


def audit_content_only(
    text: str,
    db: Session,
    images: list[str] | None = None,
) -> AuditResult:
    """仅执行审核判定，不写 audit_log。

    适用于实体尚未入库、需要先拿审核结果再创建记录的场景。
    """
    # Step 1: 敏感词扫描
    hits = _scan_sensitive_words(text, db)

    # Step 2: LLM 安全检查（受控退化：当前直接跳过）
    llm_risk = _llm_safety_check(text)

    # Step 3: 聚合风险
    all_hits = hits + ([{"word": "llm_safety", "level": llm_risk}] if llm_risk else [])
    status, reason = _aggregate_risk(all_hits)

    return AuditResult(status=status, reason=reason, matched_words=hits)


def write_audit_log_for_result(
    entity_type: str,
    entity_id: str | int,
    result: AuditResult,
    db: Session,
) -> None:
    """根据审核结果写 audit_log。passed/rejected 写，pending 不写。"""
    if result.status == "passed":
        _write_audit_log(entity_type, str(entity_id), "auto_pass", result.reason, db)
    elif result.status == "rejected":
        _write_audit_log(entity_type, str(entity_id), "auto_reject", result.reason, db)
    # pending: audit_log.action 无 pending 枚举，不写 audit_log
    # 机器审核理由由调用方写入实体的 audit_reason 字段


# ---------------------------------------------------------------------------
# 敏感词扫描
# ---------------------------------------------------------------------------

def _scan_sensitive_words(text: str, db: Session) -> list[dict]:
    """从 dict_sensitive_word 读取启用中的词，执行正则匹配。"""
    words = db.query(DictSensitiveWord).filter(
        DictSensitiveWord.enabled == 1,
    ).all()

    hits = []
    for sw in words:
        if re.search(re.escape(sw.word), text, re.IGNORECASE):
            hits.append({
                "word": sw.word,
                "level": sw.level,
                "category": sw.category,
            })
    return hits


# ---------------------------------------------------------------------------
# LLM 安全检查（接入点预留）
# ---------------------------------------------------------------------------

def _llm_safety_check(text: str) -> str | None:
    """LLM 安全检查接入点。

    当前为 no-op（受控退化）。返回 None 表示不追加风险。
    未来接入外部安全 API 时：
    - 只能提升风险等级，不能降级
    - 接口不可用时返回 None（不影响敏感词判定）
    """
    return None


# ---------------------------------------------------------------------------
# 风险等级聚合
# ---------------------------------------------------------------------------

def _aggregate_risk(hits: list[dict]) -> tuple[str, str]:
    """聚合所有命中结果，确定最终风险等级。

    high → rejected, mid → pending, low → passed (with tag)
    """
    if not hits:
        return "passed", ""

    levels = [h["level"] for h in hits if h.get("level")]
    words = [h.get("word", "") for h in hits if h.get("word")]
    reason_text = "命中敏感词: " + ", ".join(words) if words else ""

    if "high" in levels:
        return "rejected", reason_text
    if "mid" in levels:
        return "pending", reason_text
    # 只有 low
    return "passed", reason_text


# ---------------------------------------------------------------------------
# audit_log 写入
# ---------------------------------------------------------------------------

def _write_audit_log(
    target_type: str,
    target_id: str,
    action: str,
    reason: str,
    db: Session,
) -> None:
    """写入审核日志。"""
    entry = AuditLog(
        target_type=target_type,
        target_id=target_id,
        action=action,
        reason=reason or None,
        operator="system",
    )
    db.add(entry)
