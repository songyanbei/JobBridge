"""意图识别服务（Phase 3）。

识别顺序：显式命令 → show_more 同义语 → LLM IntentExtractor。
LLM 结果进入业务层前做 canonical key 校验和类型整理。
"""
import logging
import re

from app.llm import get_intent_extractor
from app.llm.base import IntentResult
from app.llm.prompts import (
    JOB_REQUIRED_FIELDS,
    RESUME_REQUIRED_FIELDS,
    SENSITIVE_SOFT_FIELDS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# §17.2 命令集 — 固定别名归并表
# ---------------------------------------------------------------------------

_COMMAND_MAP: dict[str, str] = {
    "/帮助": "help",
    "帮助": "help",
    "怎么用": "help",
    "指令": "help",
    "/重新找": "reset_search",
    "重来": "reset_search",
    "重新搜": "reset_search",
    "清空条件": "reset_search",
    "/找岗位": "switch_to_job",
    "帮我找工作": "switch_to_job",
    "切到找岗位": "switch_to_job",
    "/找工人": "switch_to_worker",
    "帮我招人": "switch_to_worker",
    "切到找工人": "switch_to_worker",
    "/删除我的信息": "delete_my_data",
    "删除信息": "delete_my_data",
    "清空我的资料": "delete_my_data",
    "注销": "delete_my_data",
    "/我的状态": "my_status",
    "我的账号状态": "my_status",
    "我被封了吗": "my_status",
    "/人工客服": "human_agent",
    "客服": "human_agent",
    "转人工": "human_agent",
    "联系人工": "human_agent",
}

# ---------------------------------------------------------------------------
# show_more 同义语
# ---------------------------------------------------------------------------

_SHOW_MORE_PATTERNS: list[str] = [
    "更多", "换一批", "下一页", "还有吗", "继续看", "再看看",
    "还有没有", "看更多", "下一批", "其他的",
]

# ---------------------------------------------------------------------------
# 合法 canonical key 集合（用于校验 criteria_patch / structured_data）
# ---------------------------------------------------------------------------

_VALID_JOB_KEYS = frozenset({
    "city", "job_category", "salary_floor_monthly", "pay_type", "headcount",
    "gender_required", "is_long_term", "district", "salary_ceiling_monthly",
    "provide_meal", "provide_housing", "dorm_condition", "shift_pattern",
    "work_hours", "accept_couple", "accept_student", "accept_minority",
    "height_required", "experience_required", "education_required",
    "rebate", "employment_type", "contract_type", "min_duration",
    "job_sub_category", "age_min", "age_max",
})

_VALID_RESUME_KEYS = frozenset({
    "expected_cities", "expected_job_categories", "salary_expect_floor_monthly",
    "gender", "age", "accept_long_term", "accept_short_term",
    "expected_districts", "height", "weight", "education", "work_experience",
    "accept_night_shift", "accept_standing_work", "accept_overtime",
    "accept_outside_province", "couple_seeking_together",
    "has_health_certificate", "ethnicity", "available_from",
    "has_tattoo", "taboo",
})

_ALL_VALID_KEYS = _VALID_JOB_KEYS | _VALID_RESUME_KEYS

_VALID_PATCH_OPS = frozenset({"add", "update", "remove"})


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def classify_intent(
    text: str,
    role: str,
    history: list[dict] | None = None,
    current_criteria: dict | None = None,
) -> IntentResult:
    """识别用户意图，按 显式命令 → show_more → LLM 的优先级。"""
    stripped = text.strip()

    # Step 1: 显式命令匹配
    cmd = _match_command(stripped)
    if cmd is not None:
        return IntentResult(
            intent="command",
            structured_data={"command": cmd},
            confidence=1.0,
        )

    # Step 2: show_more 同义语
    if _match_show_more(stripped):
        return IntentResult(intent="show_more", confidence=1.0)

    # Step 3: LLM 意图抽取
    extractor = get_intent_extractor()
    result = extractor.extract(
        text=stripped,
        role=role,
        history=history,
        current_criteria=current_criteria,
    )

    # Step 4: 校验和清洗
    return _sanitize_intent_result(result, role)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _match_command(text: str) -> str | None:
    """精确匹配命令集。返回归并后的命令名或 None。"""
    normalized = text.strip()
    return _COMMAND_MAP.get(normalized)


def _match_show_more(text: str) -> bool:
    """判断是否为 show_more 同义语。"""
    normalized = text.strip()
    for pattern in _SHOW_MORE_PATTERNS:
        if pattern in normalized:
            return True
    return False


def _sanitize_intent_result(result: IntentResult, role: str) -> IntentResult:
    """校验 LLM 返回结果，清洗不合法的字段和 patch。"""
    # 清洗 structured_data：移除未知 key
    clean_data = {}
    for k, v in result.structured_data.items():
        if k in _ALL_VALID_KEYS:
            clean_data[k] = v
        else:
            logger.warning("intent_service: dropping unknown structured_data key=%s", k)
    result.structured_data = clean_data

    # 清洗 criteria_patch：移除非法 op 或未知 field
    clean_patches = []
    for patch in result.criteria_patch:
        op = patch.get("op")
        field = patch.get("field")
        if op not in _VALID_PATCH_OPS:
            logger.warning("intent_service: dropping patch with invalid op=%s", op)
            continue
        if field not in _ALL_VALID_KEYS:
            logger.warning("intent_service: dropping patch with unknown field=%s", field)
            continue
        clean_patches.append(patch)
    result.criteria_patch = clean_patches

    # 清洗 missing_fields：只保留允许的必填字段，排除敏感软字段
    allowed_missing = JOB_REQUIRED_FIELDS | RESUME_REQUIRED_FIELDS
    clean_missing = [
        f for f in result.missing_fields
        if f in allowed_missing and f not in SENSITIVE_SOFT_FIELDS
    ]
    result.missing_fields = clean_missing

    return result
