"""意图识别服务（Phase 3）。

识别顺序：显式命令 → show_more 同义语 → LLM IntentExtractor。
LLM 结果进入业务层前做 canonical key 校验和类型整理。

Phase 7：在 LLM 调用处补 loguru 结构化打点（llm_call 事件），
便于运营追踪 provider / model / 耗时 / 状态。
"""
import logging
import re
import time

from app.config import settings
from app.core.exceptions import LLMError, LLMParseError, LLMTimeout
from app.llm import get_intent_extractor
from app.llm.base import IntentResult
from app.llm.prompts import (
    JOB_REQUIRED_FIELDS,
    RESUME_REQUIRED_FIELDS,
    SENSITIVE_SOFT_FIELDS,
)
from app.tasks.common import log_event

logger = logging.getLogger(__name__)

# intent 抽取提示词版本：随 prompts.py 改动一起 bump，便于日志回溯。
# v2.1 (Stage B)：补 job_category 闭集 + few-shot 同义词归并（餐饮/物流仓储等）。
INTENT_PROMPT_VERSION = "v2.1"

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
    "/续期": "renew_job",
    "续期": "renew_job",
    "延期": "renew_job",
    "/下架": "delist_job",
    "岗位下架": "delist_job",
    "先不招了": "delist_job",
    "暂停招聘": "delist_job",
    "/招满了": "filled_job",
    "招满了": "filled_job",
    "人招够了": "filled_job",
    "满员了": "filled_job",
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

# 允许参数的命令前缀（命令 + 空格 + 参数）
# 同义词对齐方案设计 §17.2，例如 "续15天" "续30天"
#
# 元素：(prefix, mapped_key, strict_numeric)
# - strict_numeric=True 时要求前缀后紧跟数字，避免把 "续约" / "续保" / "续杯"
#   这类无关业务词误识别为续期命令
_PARAM_COMMAND_PREFIXES: list[tuple[str, str, bool]] = [
    ("/续期", "renew_job", False),
    ("续期", "renew_job", False),
    ("延期", "renew_job", False),
    ("续", "renew_job", True),  # 兼容 "续15天"，但只接受数字紧跟
]

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
# Stage B：字段规整字典与配置
#
# 详见 docs/multi-turn-upload-stage-b-implementation.md §3.2。
# 规整层只做“类型/范围/同义词”三类轻量清洗，仍保留原 LLM 输出的字段集合，
# 不引入新 key，不替代 _sanitize_intent_result 的 canonical key 校验。
# ---------------------------------------------------------------------------

# 工种同义词 → 标准类目；prompts.py 已要求 LLM 输出 canonical 名，
# 但实际链路里偶尔仍会出现“厨师/打包/分拣”等口语化值，规整层兜底归并。
_JOB_CATEGORY_CANONICAL = frozenset({
    "电子厂", "服装厂", "食品厂", "物流仓储", "餐饮",
    "保洁", "保安", "技工", "普工", "其他",
})

_JOB_CATEGORY_SYNONYMS: dict[str, str] = {
    # 餐饮
    "厨师": "餐饮", "服务员": "餐饮", "后厨": "餐饮", "饭店": "餐饮",
    "餐厅": "餐饮", "帮厨": "餐饮", "传菜": "餐饮", "厨房": "餐饮",
    # 物流仓储
    "打包": "物流仓储", "打包工": "物流仓储", "分拣": "物流仓储",
    "分拣员": "物流仓储", "仓库": "物流仓储", "仓管": "物流仓储",
    "快递": "物流仓储", "装卸": "物流仓储", "拣货": "物流仓储",
    # 普工
    "操作工": "普工", "产线": "普工", "流水线": "普工", "计件工": "普工",
    # 电子厂
    "smt": "电子厂", "组装": "电子厂", "质检": "电子厂", "焊锡": "电子厂",
    # 保洁
    "清洁": "保洁", "客房清洁": "保洁", "保洁阿姨": "保洁",
    # 保安
    "门岗": "保安", "巡逻": "保安", "保安员": "保安",
    # 服装厂
    "缝纫": "服装厂", "车工": "服装厂", "锁眼": "服装厂",
    # 食品厂
    "烘焙": "食品厂", "糕点": "食品厂",
}

_LIST_FIELDS = frozenset({
    "city", "job_category", "expected_cities", "expected_job_categories",
})

_INT_FIELDS = frozenset({
    "salary_floor_monthly", "salary_ceiling_monthly",
    "salary_expect_floor_monthly", "headcount", "age",
    "age_min", "age_max",
})

_HEADCOUNT_MAX = 9999
_SALARY_MIN = 500
_SALARY_MAX = 200_000
_AGE_MIN = 14
_AGE_MAX = 80


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def classify_intent(
    text: str,
    role: str,
    history: list[dict] | None = None,
    current_criteria: dict | None = None,
    user_msg_id: str | None = None,
) -> IntentResult:
    """识别用户意图，按 显式命令 → show_more → LLM 的优先级。

    Phase 7：LLM 调用结构化打点（``llm_call``）含
    provider / model / prompt_version / input_tokens / output_tokens /
    duration_ms / intent / user_msg_id / status。
    parse_failed 不再 raise，而是回落到 chitchat 以保持调用方（message_router）
    的现有容错路径；status 会反映真实失败类型以便运维分析。
    """
    stripped = text.strip()

    # Step 1: 显式命令匹配
    cmd_result = _match_command(stripped)
    if cmd_result is not None:
        cmd, args = cmd_result
        data: dict = {"command": cmd}
        if args:
            data["args"] = args
        return IntentResult(
            intent="command",
            structured_data=data,
            confidence=1.0,
        )

    # Step 2: show_more 同义语
    if _match_show_more(stripped):
        return IntentResult(intent="show_more", confidence=1.0)

    # Step 3: LLM 意图抽取（带结构化打点）
    extractor = get_intent_extractor()
    start = time.perf_counter()
    status = "ok"
    result: IntentResult | None = None
    parse_failed = False
    try:
        result = extractor.extract(
            text=stripped,
            role=role,
            history=history,
            current_criteria=current_criteria,
        )
    except LLMTimeout:
        status = "timeout"
        raise
    except LLMParseError as exc:
        # parse_failed：日志要记真实状态，但业务链路降级到 chitchat 以保持连续。
        # provider 在 raise 前已把本次请求的 token 用量挂到 exc.input_tokens /
        # exc.output_tokens 上，这里回读到 fallback IntentResult 以便 log_event 记录
        # 真实 token 用量（即使解析失败也能进账本）。
        status = "parse_failed"
        parse_failed = True
        result = IntentResult(
            intent="chitchat",
            confidence=0.0,
            input_tokens=getattr(exc, "input_tokens", None),
            output_tokens=getattr(exc, "output_tokens", None),
        )
    except LLMError:
        status = "http_error"
        raise
    except Exception:
        # 非 LLMError 家族的意外异常（如 provider 实现 bug、类型错误等）。
        # 单独打 unknown_error 便于日志归因与告警分级。
        status = "unknown_error"
        raise
    finally:
        log_event(
            "llm_call",
            call_site="intent_extract",
            provider=settings.llm_provider,
            model=settings.llm_intent_model,
            prompt_version=INTENT_PROMPT_VERSION,
            duration_ms=int((time.perf_counter() - start) * 1000),
            intent=getattr(result, "intent", None),
            input_tokens=getattr(result, "input_tokens", None),
            output_tokens=getattr(result, "output_tokens", None),
            user_msg_id=user_msg_id,
            status=status,
        )

    # parse_failed 的 fallback result 不需要再 sanitize（全空结构）
    if parse_failed:
        return result
    # Step 4: 校验和清洗
    return _sanitize_intent_result(result, role)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _match_command(text: str) -> tuple[str, str] | None:
    """匹配命令集，返回 (归并命令 key, 参数字符串) 或 None。

    处理三种形态：
    1. 精确匹配 `_COMMAND_MAP`，参数为空
    2. 带空格参数，如 "/续期 15"
    3. 粘连参数形态，如 "续15天"（仅 `_PARAM_COMMAND_PREFIXES` 中登记的前缀）
    """
    normalized = text.strip()
    if not normalized:
        return None

    # 1) 精确匹配
    cmd = _COMMAND_MAP.get(normalized)
    if cmd is not None:
        return (cmd, "")

    # 2) 带空格参数："/续期 15"
    if " " in normalized:
        prefix, _, args = normalized.partition(" ")
        cmd = _COMMAND_MAP.get(prefix)
        if cmd is not None:
            return (cmd, args.strip())

    # 3) 粘连参数形态："续15天" / "续期15"
    for prefix, mapped, strict_numeric in _PARAM_COMMAND_PREFIXES:
        if not normalized.startswith(prefix):
            continue
        if len(normalized) <= len(prefix):
            continue
        rest = normalized[len(prefix):].strip()
        if not rest:
            continue
        # 宽泛前缀（如 "续"）要求紧跟数字，否则 "续约" / "续保" 会被误识
        if strict_numeric and not rest[0].isdigit():
            continue
        return (mapped, rest)

    return None


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

    # Stage B：字段规整层（类型/范围/同义词归并），详见 §3.2。
    result.structured_data = _normalize_structured_data(
        result.structured_data, role, result.intent,
    )
    result.criteria_patch = _normalize_criteria_patch(result.criteria_patch)

    return result


# ---------------------------------------------------------------------------
# Stage B：字段规整 helpers
# ---------------------------------------------------------------------------

def _normalize_job_category_value(value):
    """单个 job_category 值映射到标准类目；无法映射时保留原值。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    if text in _JOB_CATEGORY_CANONICAL:
        return text
    mapped = _JOB_CATEGORY_SYNONYMS.get(text) or _JOB_CATEGORY_SYNONYMS.get(text.lower())
    if mapped:
        return mapped
    # 子串包含命中（处理“厨师A岗 / 包装/分拣组”这类拼接表达）。
    for syn, canonical in _JOB_CATEGORY_SYNONYMS.items():
        if syn in text or syn in text.lower():
            return canonical
    logger.warning("intent_service: unknown job_category=%s, keep as-is", text)
    return text


def _normalize_string_list(value) -> list[str]:
    """把 str / list / None 统一成去空去重后的 list[str]。"""
    if value is None:
        return []
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        s = str(item).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _normalize_int_field(value, *, lo: int | None = None, hi: int | None = None):
    """尝试把字段转 int 并落在 [lo, hi] 区间内；非法值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if lo is not None and v < lo:
        return None
    if hi is not None and v > hi:
        return None
    return v


def _coerce_field_value(field: str, value, *, force_list: bool):
    """按字段语义把单个 value 规整成最终类型。

    force_list=True：上层希望该字段始终落在 list（搜索 criteria 用法）。
    force_list=False：上层希望该字段落在标量（上传 structured_data 用法）。
    """
    # 列表型字段
    if field in {"city", "expected_cities"}:
        items = _normalize_string_list(value)
        if force_list:
            return items
        return items[0] if items else None

    if field in {"job_category", "expected_job_categories"}:
        items = _normalize_string_list(value)
        mapped: list[str] = []
        seen: set[str] = set()
        for item in items:
            m = _normalize_job_category_value(item)
            if not m or m in seen:
                continue
            seen.add(m)
            mapped.append(m)
        if force_list:
            return mapped
        return mapped[0] if mapped else None

    # int 字段
    if field == "headcount":
        return _normalize_int_field(value, lo=1, hi=_HEADCOUNT_MAX)
    if field == "age":
        return _normalize_int_field(value, lo=_AGE_MIN, hi=_AGE_MAX)
    if field in {"age_min", "age_max"}:
        return _normalize_int_field(value, lo=_AGE_MIN, hi=_AGE_MAX)
    if field in {"salary_floor_monthly", "salary_ceiling_monthly", "salary_expect_floor_monthly"}:
        return _normalize_int_field(value, lo=_SALARY_MIN, hi=_SALARY_MAX)

    return value


def _normalize_structured_data(data: dict, role: str, intent: str) -> dict:
    """对 structured_data 做类型 / 范围 / 同义词归并。

    - 上传场景（upload_*）：list 字段归一为 str（与 Job/Resume 列定义对齐，
      但 expected_* 仍保留 list；它们在 Resume 表里就是 JSON list）。
    - 搜索场景（search_*/follow_up）：city / job_category 始终落 list。
    - 非法 int / 不在范围 → 字段直接丢弃，避免污染下游查询或入库。
    - salary_ceiling < salary_floor → 丢弃 ceiling 并记 warning。
    """
    if not data:
        return {}

    upload_intent = intent in ("upload_job", "upload_resume", "upload_and_search")

    out: dict = {}
    for key, raw in data.items():
        if key not in _ALL_VALID_KEYS:
            continue

        # expected_* 永远是 list
        if key in {"expected_cities", "expected_job_categories"}:
            coerced = _coerce_field_value(key, raw, force_list=True)
            if coerced:
                out[key] = coerced
            continue

        # city / job_category：搜索按 list；上传按 str
        if key in {"city", "job_category"}:
            force_list = not upload_intent
            coerced = _coerce_field_value(key, raw, force_list=force_list)
            if coerced not in (None, [], ""):
                out[key] = coerced
            continue

        # 数值字段：丢弃非法值
        if key in _INT_FIELDS:
            coerced = _coerce_field_value(key, raw, force_list=False)
            if coerced is not None:
                out[key] = coerced
            else:
                logger.warning(
                    "intent_service: drop invalid int field=%s raw=%r", key, raw,
                )
            continue

        out[key] = raw

    # salary_ceiling < salary_floor：丢 ceiling
    floor = out.get("salary_floor_monthly")
    ceiling = out.get("salary_ceiling_monthly")
    if floor is not None and ceiling is not None and ceiling < floor:
        logger.warning(
            "intent_service: drop salary_ceiling=%s < floor=%s", ceiling, floor,
        )
        out.pop("salary_ceiling_monthly", None)

    return out


def _normalize_criteria_patch(patches: list[dict]) -> list[dict]:
    """对 criteria_patch 做与 structured_data 一致的字段规整。

    搜索路径上的 patch field 几乎都属于 list 型（city / job_category / expected_*），
    因此统一按 force_list=True 处理；标量字段（如 salary_floor_monthly / age）保持标量。
    op == "remove" 且 value 为 None 时不再额外规整 value。
    """
    if not patches:
        return []
    out: list[dict] = []
    for patch in patches:
        field = patch.get("field")
        op = patch.get("op")
        value = patch.get("value")

        if field not in _ALL_VALID_KEYS:
            continue

        # remove + value=None 表示删除整个字段，原样保留。
        if op == "remove" and value is None:
            out.append({"op": op, "field": field, "value": None})
            continue

        if field in _LIST_FIELDS:
            coerced = _coerce_field_value(field, value, force_list=True)
            if not coerced:
                # 空列表无意义，跳过
                continue
            out.append({"op": op, "field": field, "value": coerced})
            continue

        if field in _INT_FIELDS:
            coerced = _coerce_field_value(field, value, force_list=False)
            if coerced is None:
                logger.warning(
                    "intent_service: drop invalid int patch field=%s value=%r",
                    field, value,
                )
                continue
            out.append({"op": op, "field": field, "value": coerced})
            continue

        out.append({"op": op, "field": field, "value": value})
    return out
