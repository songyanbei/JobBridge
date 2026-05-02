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
    DIALOGUE_PROMPT_VERSION,
    INTENT_PROMPT_VERSION,
    JOB_REQUIRED_FIELDS,
    RESUME_REQUIRED_FIELDS,
    SEARCH_JOB_MIN_FIELDS,
    SENSITIVE_SOFT_FIELDS,
)
from app.tasks.common import log_event

logger = logging.getLogger(__name__)

# INTENT_PROMPT_VERSION 单一来源是 app.llm.prompts；这里通过 import 直接复用，
# 避免本文件与 prompts.py 里的版本号双写时 drift（reviewer P3：曾出现过两份
# 版本号不一致，导致 message_router 落库的 prompt_version 与实际 prompt 不匹配）。
# 完整版本变更日志见 prompts.py 头部。

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
    # Stage B P1-2：注册 /取消 为命令以闭合 LLM 误判 intent=command 的旁路。
    # 自然语言 "取消"/"算了" 等仍由 message_router._is_cancel 在 pending guard
    # 内处理（见 docs/multi-turn-upload-session-state.md §9.3）。
    "/取消": "cancel_pending",
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

# 阶段三 P2：合法字段集合的真源已迁移到 app.dialogue.slot_schema。
# 这里保留常量名（外部兼容）但**只是 bootstrap 默认值**：模块底部的
# _bootstrap_field_constants_from_schema() 会在所有定义就绪后用 schema 派生
# 覆盖这些 globals。这样：
#  - 修 schema 立刻反映到运行时 _normalize_structured_data 的过滤集；
#  - schema 内的 display 占位字段（如 job_title）不会被 silently dropped；
#  - 模块内部 bare-name 引用（_ALL_VALID_KEYS / _LIST_FIELDS 等）正常生效。
# 阶段四清理时直接删这一段，所有引用走 slot_schema 即可。

_VALID_JOB_KEYS: frozenset[str] = frozenset()  # bootstrap-overridden
_VALID_RESUME_KEYS: frozenset[str] = frozenset()  # bootstrap-overridden
_ALL_VALID_KEYS: frozenset[str] = frozenset()  # bootstrap-overridden

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

# 阶段三 P2：_LIST_FIELDS / _INT_FIELDS 也由 schema 派生（bootstrap 覆盖）。
# 这里仍声明默认值，避免本模块底部 bootstrap 之前就被引用而抛 NameError。
_LIST_FIELDS: frozenset[str] = frozenset({
    "city", "job_category", "expected_cities", "expected_job_categories",
})

_INT_FIELDS: frozenset[str] = frozenset({
    "salary_floor_monthly", "salary_ceiling_monthly",
    "salary_expect_floor_monthly", "headcount", "age",
    "age_min", "age_max",
})

_HEADCOUNT_MAX = 9999
_SALARY_MIN = 500
_SALARY_MAX = 200_000
_AGE_MIN = 14
_AGE_MAX = 80

# Bug 4：搜索 / follow_up 上 expected_* → city / job_category 的兜底重映射。
# 即使 prompt 已明确，仍保留服务端兜底以应对 LLM 漂移；不做反向映射（上传简历
# 时 LLM 输出 city 是它自己的事，由上传规整逻辑决定，不在这层强行改写）。
_SEARCH_INTENTS = frozenset({"search_job", "search_worker", "follow_up"})
_SEARCH_FIELD_REMAP = {
    "expected_cities": "city",
    "expected_job_categories": "job_category",
}

_SEARCH_MISSING_FIELDS = frozenset({
    "city", "job_category", "salary_floor_monthly",
})

# Phase 4 (PR1)：worker 搜索护栏的两组信号 — **fallback-only**。
# v2 主路径（DialogueParseResult → reducer）由 prompt + reducer 在结构上保证
# worker 不会落 upload_job；这两组常量仅作 _classify_intent_legacy 内核兜底，
# 用于 LLM 漂移导致 worker 角色被错判 upload_job 时强制纠回 search_job。
# 详见 docs/dialogue-intent-extraction-phased-plan.md §4.1.3。
_WORKER_SEARCH_SIGNALS = (
    "找", "想找", "找个", "找份", "求职", "工作", "岗位", "活", "上班",
    "想做", "能做", "有吗", "有没有",
)

_JOB_POSTING_SIGNALS = (
    "招聘", "招工", "招人", "急招", "招募", "招几个", "招一", "招两",
    "招二", "招三", "招四", "招五", "要人", "缺人",
)

# Phase 4 (PR1)：删除 _CITY_ADD_SIGNALS / _CITY_REPLACE_SIGNALS（自阶段一引入起
# 全仓库 0 引用，属于历史死代码，不影响任何路径行为）。
_CITY_FOLLOW_UP_MAX_LEN = 12

# Bug 4：城市字典归一缓存（短名 / aliases → 规范名）。
# 进程级 lazy load；admin 改 dict_city.aliases 后需重启或调用 _clear_city_lookup_cache()
# 才生效。dict_city 表内容稳定（340 城），不为这点写监听器。
_CITY_LOOKUP_CACHE: dict[str, str] | None = None
_COMMON_CITY_ALIASES: dict[str, str] = {
    "北京": "北京市", "北京市": "北京市",
    "上海": "上海市", "上海市": "上海市",
    "广州": "广州市", "广州市": "广州市",
    "深圳": "深圳市", "深圳市": "深圳市",
    "苏州": "苏州市", "苏州市": "苏州市",
    "昆山": "昆山市", "昆山市": "昆山市",
    "无锡": "无锡市", "无锡市": "无锡市",
    "南京": "南京市", "南京市": "南京市",
    "杭州": "杭州市", "杭州市": "杭州市",
    "宁波": "宁波市", "宁波市": "宁波市",
    "合肥": "合肥市", "合肥市": "合肥市",
    "重庆": "重庆市", "重庆市": "重庆市",
    "成都": "成都市", "成都市": "成都市",
    "天津": "天津市", "天津市": "天津市",
    "武汉": "武汉市", "武汉市": "武汉市",
    "西安": "西安市", "西安市": "西安市",
    "郑州": "郑州市", "郑州市": "郑州市",
    "青岛": "青岛市", "青岛市": "青岛市",
    "济南": "济南市", "济南市": "济南市",
    "厦门": "厦门市", "厦门市": "厦门市",
    "福州": "福州市", "福州市": "福州市",
    "长沙": "长沙市", "长沙市": "长沙市",
}


def _get_city_lookup() -> dict[str, str]:
    """加载 dict_city：name + short_name + aliases 都映射到规范名 (name)。

    DB 不可用（如某些 unit test 环境）时返回空 dict 且不缓存，下次调用再重试。
    """
    global _CITY_LOOKUP_CACHE
    if _CITY_LOOKUP_CACHE is not None:
        return _CITY_LOOKUP_CACHE
    try:
        from app.db import SessionLocal
        from app.models import DictCity
        with SessionLocal() as db:
            rows = db.query(DictCity).filter(DictCity.enabled == 1).all()
        mapping: dict[str, str] = {}
        for c in rows:
            if not c.name:
                continue
            mapping.setdefault(c.name, c.name)
            if c.short_name:
                mapping.setdefault(c.short_name, c.name)
            for alias in (c.aliases or []):
                if isinstance(alias, str) and alias.strip():
                    mapping.setdefault(alias.strip(), c.name)
        _CITY_LOOKUP_CACHE = mapping
        return mapping
    except Exception as exc:  # noqa: BLE001 — 兜底容错，DB 异常不应阻塞 intent 流
        logger.warning("intent_service: load city dict failed: %s", exc)
        return {}


def _clear_city_lookup_cache() -> None:
    """供测试 / 运营改完 dict_city 后清缓存。"""
    global _CITY_LOOKUP_CACHE
    _CITY_LOOKUP_CACHE = None


def _normalize_city_value(value):
    """单个城市值归一：dict_city 短名 / aliases → 规范名。无映射保留原值。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return text
    return _get_city_lookup().get(text) or _COMMON_CITY_ALIASES.get(text, text)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def classify_intent(
    text: str,
    role: str,
    history: list[dict] | None = None,
    current_criteria: dict | None = None,
    user_msg_id: str | None = None,
    session_hint: dict | None = None,
) -> IntentResult:
    """识别用户意图（legacy 入口）。

    阶段二（dialogue-intent-extraction-phased-plan §2）起，此函数退化为
    _classify_intent_legacy 的薄包装；新链路统一走 classify_dialogue 入口。
    保留这个名字是为了向后兼容 admin / 老代码 import，**不再带 v2 分支**，
    避免 v2 路径回退时递归。
    """
    return _classify_intent_legacy(
        text=text,
        role=role,
        history=history,
        current_criteria=current_criteria,
        user_msg_id=user_msg_id,
        session_hint=session_hint,
    )


def _classify_intent_legacy(
    text: str,
    role: str,
    history: list[dict] | None = None,
    current_criteria: dict | None = None,
    user_msg_id: str | None = None,
    session_hint: dict | None = None,
) -> IntentResult:
    """legacy 内核（阶段一行为）：显式命令 → show_more → LLM IntentExtractor.extract。

    Phase 7：LLM 调用结构化打点（``llm_call``）含
    provider / model / prompt_version / input_tokens / output_tokens /
    duration_ms / intent / user_msg_id / status。
    parse_failed 不再 raise，而是回落到 chitchat 以保持调用方（message_router）
    的现有容错路径；status 会反映真实失败类型以便运维分析。

    阶段二 classify_dialogue 在 v2 fallback 路径上**直接调用本函数内核**，
    避免回到带 v2 分支的入口产生递归。
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
            session_hint=session_hint,
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
            # Stage C1：仅记录 hint 包含哪些键，方便日后比较有/无 hint 的抽取质量
            session_hint_keys=sorted(session_hint.keys()) if session_hint else None,
        )

    # parse_failed 的 fallback result 不需要再 sanitize（全空结构）
    if parse_failed:
        return result
    # Step 4: 校验和清洗。Phase 4 (PR1) 显式两步：
    #   1. _apply_worker_intent_guard：worker upload_job → search_job 的 fallback 护栏
    #      （v2 主路径不会调用，由此结构性禁止误调用）
    #   2. _sanitize_common：schema 派生的字段清洗（v2 派生路径如需可单独调用）
    result = _apply_worker_intent_guard(result, role, raw_text=stripped)
    return _sanitize_common(result, role)


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


def _hits_worker_search_signal(text: str) -> bool:
    """worker + 找工/求职信号命中。**Phase 4 (PR1)：fallback-only**。"""
    if not text:
        return False
    return any(s in text for s in _WORKER_SEARCH_SIGNALS)


def _hits_job_posting_signal(text: str) -> bool:
    """招聘 / 发岗位信号命中。**Phase 4 (PR1)：fallback-only**。"""
    if not text:
        return False
    return any(s in text for s in _JOB_POSTING_SIGNALS)


def _should_force_worker_search(role: str, text: str, intent: str) -> bool:
    """worker 角色把 LLM 误判的 upload_job 强制纠回 search_job。

    判据（详见 dialogue-intent-extraction-phased-plan §1.1）：
    1. role == "worker"
    2. intent == "upload_job"（worker 永远不能发布岗位）
    3. 文本命中 _WORKER_SEARCH_SIGNALS（找/想找/求职/工作/打工/上班 等）
    4. 不命中 _JOB_POSTING_SIGNALS（招聘/招工/招人 等显式发布信号）

    **Phase 4 (PR1)**：仅 _classify_intent_legacy 内核（经 _apply_worker_intent_guard）
    调用；v2 主路径不依赖此护栏。
    """
    if role != "worker":
        return False
    if intent != "upload_job":
        return False
    if not _hits_worker_search_signal(text):
        return False
    if _hits_job_posting_signal(text):
        return False
    return True


def _apply_worker_intent_guard(
    result: IntentResult,
    role: str,
    raw_text: str,
) -> IntentResult:
    """worker 误判 upload_job → search_job 的强制纠正。**Phase 4：fallback-only**。

    Phase 4 (PR1) 从 _sanitize_intent_result 抽离：让 _classify_intent_legacy 内核
    显式两步处理（先纠 intent，再做字段清洗），并在 v2 主路径上结构性禁止误调用。
    v2 主链路（classify_dialogue 的 dual_read / primary 派生路径）由 prompt + reducer
    接管 worker 误判，**不**调用本函数。
    """
    if _should_force_worker_search(role, raw_text, result.intent):
        logger.warning(
            "intent_service: worker search guardrail corrects intent "
            "upload_job -> search_job (text=%r)",
            raw_text,
        )
        result.intent = "search_job"
        # upload_job 路径下 LLM 通常输出标量 city / job_category 与 headcount / pay_type;
        # 把可复用搜索字段保留，发布相关字段交由后续 _normalize_structured_data 在
        # search_intent 分支按 list 强制；非搜索字段会被合法字段集合自然 drop。
        # missing_fields 强制清空，由后端按搜索 schema 重算。
        result.missing_fields = []
    return result


def _sanitize_common(result: IntentResult, role: str) -> IntentResult:
    """schema 派生的字段清洗（structured_data / criteria_patch / missing_fields 归一）。

    Phase 4 (PR1) 从 _sanitize_intent_result 抽离：**不**包含 worker 搜索护栏。
    legacy 路径继续由 _sanitize_intent_result 包装器一次性串起 worker guard + 本函数。

    **当前未被任何 v2 派生路径调用**：v2 走 reducer + slot_schema.validate_slots_delta
    已内置等价清洗，dialogue_compat/dialogue_reducer/dialogue_applier 都不调本函数。
    后续 PR3 评估若 v2 派生 IntentResult 需要本函数同款清洗时再接通；接通前请保持
    现状以避免重复清洗或语义偏移。

    注意：本函数 in-place mutate 输入 result（structured_data / criteria_patch /
    missing_fields 直接赋值到原对象）。调用方若要保留原 result，请先 copy。
    """
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
    # Bug 4：搜索 intent 把 expected_* 重映射，避免追问"期望城市"但 session.search_criteria
    # 已有 city 时 _compute_search_missing 比不上的死循环。
    search_intent = result.intent in _SEARCH_INTENTS
    allowed_missing = JOB_REQUIRED_FIELDS | RESUME_REQUIRED_FIELDS
    clean_missing: list[str] = []
    seen_missing: set[str] = set()
    for f in result.missing_fields:
        target = _SEARCH_FIELD_REMAP.get(f, f) if search_intent else f
        if target not in allowed_missing or target in SENSITIVE_SOFT_FIELDS:
            continue
        if target in seen_missing:
            continue
        seen_missing.add(target)
        clean_missing.append(target)
    result.missing_fields = clean_missing

    # Stage B：字段规整层（类型/范围/同义词归并），详见 §3.2。
    result.structured_data = _normalize_structured_data(
        result.structured_data, role, result.intent,
    )
    result.criteria_patch = _normalize_criteria_patch(
        result.criteria_patch, result.intent,
    )

    return result


def _sanitize_intent_result(
    result: IntentResult,
    role: str,
    raw_text: str = "",
) -> IntentResult:
    """legacy 入口的字段清洗 wrapper：worker 护栏 + schema 派生清洗。

    Phase 4 (PR1)：保留旧名作为向后兼容入口（test_intent_service / test_phase1 /
    golden runner / dev_rollout 测试均按此名调用）。内部组合两步：

    1. _apply_worker_intent_guard：worker 误判 upload_job → search_job 的 fallback 护栏。
    2. _sanitize_common：schema 派生的字段清洗 / 类型规整。

    v2 主路径（classify_dialogue dual_read / primary 派生）**不**调用本 wrapper；
    需要等价清洗时直接调 _sanitize_common。
    """
    result = _apply_worker_intent_guard(result, role, raw_text)
    return _sanitize_common(result, role)


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
        # Bug 4：dict_city 短名/aliases → 规范名，避免 JSON_CONTAINS 字面量比对漏召回
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            n = _normalize_city_value(item)
            if not n or n in seen:
                continue
            seen.add(n)
            normalized.append(n)
        if force_list:
            return normalized
        return normalized[0] if normalized else None

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
    - Bug 4：搜索 intent 上 LLM 误把值塞到 expected_cities / expected_job_categories
      时（broker /找工人 时高发），重映射到 city / job_category，避免 _query_resumes
      读不到字段直接 0 召回。
    - 非法 int / 不在范围 → 字段直接丢弃，避免污染下游查询或入库。
    - salary_ceiling < salary_floor → 丢弃 ceiling 并记 warning。
    """
    if not data:
        return {}

    upload_intent = intent in ("upload_job", "upload_resume", "upload_and_search")
    search_intent = intent in _SEARCH_INTENTS

    # Bug 4：搜索 intent 把 expected_* 重映射到 canonical 搜索字段。
    # 同名键已存在时合并而非覆盖（保 LLM 同时给两份的边界情形）。
    if search_intent:
        remapped: dict = {}
        for key, raw in data.items():
            target = _SEARCH_FIELD_REMAP.get(key, key)
            if target in remapped:
                # 合并 list；标量直接保留首次出现的值
                existing = remapped[target]
                if isinstance(existing, list) and isinstance(raw, list):
                    for v in raw:
                        if v not in existing:
                            existing.append(v)
                # 其余情况以已有值为准，避免覆盖更结构化的输入
            else:
                remapped[target] = raw
            if key != target:
                logger.warning(
                    "intent_service: remap search field %s -> %s (intent=%s)",
                    key, target, intent,
                )
        data = remapped

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


def _normalize_criteria_patch(
    patches: list[dict],
    intent: str | None = None,
) -> list[dict]:
    """对 criteria_patch 做与 structured_data 一致的字段规整。

    搜索路径上的 patch field 几乎都属于 list 型（city / job_category / expected_*），
    因此统一按 force_list=True 处理；标量字段（如 salary_floor_monthly / age）保持标量。
    op == "remove" 且 value 为 None 时不再额外规整 value。

    Bug 4：搜索 intent 上把 patch.field 的 expected_* 重映射到 city / job_category。
    intent 缺省（旧调用方）保持原行为不动。
    """
    if not patches:
        return []
    search_intent = intent in _SEARCH_INTENTS
    out: list[dict] = []
    for patch in patches:
        field = patch.get("field")
        op = patch.get("op")
        value = patch.get("value")

        if search_intent and field in _SEARCH_FIELD_REMAP:
            mapped = _SEARCH_FIELD_REMAP[field]
            logger.warning(
                "intent_service: remap patch field %s -> %s (intent=%s)",
                field, mapped, intent,
            )
            field = mapped

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


# ---------------------------------------------------------------------------
# Stage C1：session_hint 构造（spec §2.11）
# ---------------------------------------------------------------------------

def build_session_hint(session) -> dict:
    """根据当前 session 构造给 LLM 的 session_hint。

    Phase 1（dialogue-intent-extraction-phased-plan §1.1）：在 C1 占位 hint 之上
    补 awaiting_fields / awaiting_frame / awaiting_expires_at 与 search_criteria 摘要，
    让 provider 把会话状态以结构化键值拼进 system prompt。
    """
    if session is None:
        return {}
    pending_upload = dict(getattr(session, "pending_upload", {}) or {})
    search_criteria = dict(getattr(session, "search_criteria", {}) or {})
    return {
        "active_flow": getattr(session, "active_flow", None),
        # 上传草稿追问字段（保留旧契约）
        "awaiting_field": getattr(session, "awaiting_field", None),
        "pending_upload_intent": getattr(session, "pending_upload_intent", None),
        "pending_upload": pending_upload,
        # Phase 1 新增：搜索 awaiting + 当前累积 search_criteria 摘要
        "awaiting_fields": list(getattr(session, "awaiting_fields", []) or []),
        "awaiting_frame": getattr(session, "awaiting_frame", None),
        "awaiting_expires_at": getattr(session, "awaiting_expires_at", None),
        "search_criteria": search_criteria,
        "broker_direction": getattr(session, "broker_direction", None),
    }


# ---------------------------------------------------------------------------
# Phase 1：临时 legacy schema helpers（dialogue-intent-extraction-phased-plan §1.3.bis）
#
# 不新建 schema 文件；按 frame 包一层 helper：
#   - job_search:        required_all = {city, job_category}
#   - candidate_search:  required_any = {city, job_category}（任一即可）
#   - job_upload:        required_all = JOB_REQUIRED_FIELDS
#   - resume_upload:     required_all = RESUME_REQUIRED_FIELDS
#
# 阶段三用统一 slot schema 替换时只换内部实现，调用方不动。
# ---------------------------------------------------------------------------

# 阶段三：所有 frame → 字段集 / 必填集 / missing 算法收口到
# app.dialogue.slot_schema。这里三个 _legacy_* helper 保留外部签名，
# 内部改为调 schema，方便阶段二/阶段三/阶段四调用方零改动。
# 旧 _LEGACY_JOB_SEARCH_FIELDS / _LEGACY_CANDIDATE_SEARCH_FIELDS 常量
# 已迁移到 slot_schema 内部组装；本文件不再保留。

def _legacy_required(frame: str) -> tuple[frozenset[str], frozenset[str]]:
    """返回 (required_all, required_any)（阶段三委托 slot_schema.required_for）。"""
    from app.dialogue import slot_schema as _ss
    return _ss.required_for(frame)


def _legacy_valid_fields(frame: str) -> frozenset[str]:
    """frame 的合法字段集合（阶段三委托 slot_schema.fields_for）。"""
    from app.dialogue import slot_schema as _ss
    return _ss.fields_for(frame)


def _legacy_compute_missing(frame: str, criteria: dict) -> list[str]:
    """按 (required_all, required_any) 算 missing（阶段三委托 slot_schema）。"""
    from app.dialogue import slot_schema as _ss
    return _ss.compute_missing_slots(frame, criteria)


# ---------------------------------------------------------------------------
# 阶段二：classify_dialogue（dialogue-intent-extraction-phased-plan §2.3）
# ---------------------------------------------------------------------------

import hashlib as _hashlib
import random as _random
from dataclasses import dataclass as _dataclass


@_dataclass
class DialogueRouteResult:
    """classify_dialogue 返回值。

    source 取值（与 phased-plan §2.3 对齐）：
    - legacy：mode=off / shadow 主路由，或 dual_read 但用户未命中白名单/桶
    - v2_shadow：mode=shadow 旁路调 v2，但主路由仍走 legacy（仅写日志）
    - v2_dual_read：dual_read 命中且 v2 解析成功，主路由走 v2 派生
    - v2_fallback_legacy：dual_read 命中但 v2 解析失败，已回退到 legacy
    """
    intent_result: IntentResult
    decision: object | None  # DialogueDecision 类型，避免循环 import
    source: str


def _hash_to_bucket(userid: str, total_buckets: int = 100) -> int:
    """userid → 0..99 桶号；与 dialogue_v2_hash_buckets 对比决定是否命中灰度。"""
    if not userid or total_buckets <= 0:
        return total_buckets  # 永不命中
    h = _hashlib.md5(userid.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % total_buckets


def _emit_dialogue_v2_parse(
    parse,
    *,
    user_msg_id: str | None,
    mode: str,
) -> None:
    """phased-plan §2.3 三类事件之一：dialogue_v2_parse（成功 parse 的原始字段）。

    与 dialogue_v2_decision / dialogue_v2_legacy_diff 解耦，便于分析师独立追踪：
    - parse 成功率 / dialogue_act 分布（只需要 dialogue_v2_parse）
    - decision 分布（dialogue_v2_decision，含 reducer 裁决后 state_transition）
    - shadow 与 legacy 的差异（dialogue_v2_legacy_diff，含 legacy_intent 对照）

    在 dual_read 与 shadow 两条路径上都先发本事件，再发各自的 decision/diff。
    parse 失败会走 dialogue_v2_parse_error / dialogue_v2_fallback_to_legacy，
    本函数不消费失败路径。
    """
    log_event(
        "dialogue_v2_parse",
        user_msg_id=user_msg_id,
        mode=mode,
        dialogue_act=parse.dialogue_act,
        frame_hint=parse.frame_hint,
        slots_delta_keys=sorted(parse.slots_delta.keys()) if parse.slots_delta else [],
        merge_hint_keys=sorted(parse.merge_hint.keys()) if parse.merge_hint else [],
        needs_clarification=bool(parse.needs_clarification),
        confidence=parse.confidence,
        conflict_action=parse.conflict_action,
        prompt_version=DIALOGUE_PROMPT_VERSION,
        input_tokens=parse.input_tokens,
        output_tokens=parse.output_tokens,
    )


def _is_dual_read_target(userid: str) -> bool:
    if not userid:
        return False
    if userid in settings.dialogue_v2_userid_whitelist_set:
        return True
    buckets = getattr(settings, "dialogue_v2_hash_buckets", 0) or 0
    if buckets <= 0:
        return False
    return _hash_to_bucket(userid) < buckets


def classify_dialogue(
    text: str,
    role: str,
    history: list[dict] | None = None,
    *,
    session=None,
    user_msg_id: str | None = None,
    userid: str | None = None,
) -> DialogueRouteResult:
    """阶段二统一意图入口。按 dialogue_v2_mode 决定走 legacy / shadow / dual_read。

    依赖 SessionState（不强制类型注解避免循环 import）：用于 build_session_hint /
    reducer 读 awaiting / merge policy。session=None 时一律走 legacy（避免破坏
    没有 session 的旧调用点）。

    实现要点：
    - mode=off：直接走 _classify_intent_legacy，零 v2 调用。
    - mode=shadow：legacy 主路由 + 按 sample_rate 旁路调 v2 写日志（仅日志）。
    - mode=dual_read：白名单 / hash 桶命中走 v2 派生；未命中走 legacy。
    - 任何 v2 失败（NotImplementedError / LLMParseError / 任意异常）都 fallback
      到 _classify_intent_legacy 内核（**不**再调本函数自身或 classify_intent）。
    """
    from app.services.dialogue_compat import decision_to_intent_result
    from app.services.dialogue_reducer import reduce as _reduce

    stripped = text.strip()
    current_criteria = (
        dict(getattr(session, "search_criteria", {}) or {}) if session else None
    )
    session_hint = build_session_hint(session) if session else None

    mode = getattr(settings, "dialogue_v2_mode", "off")

    # 阶段二（dialogue-intent-extraction-phased-plan §2.5.5）优先级硬约束：
    # 显式斜杠命令 > 后端状态约束 > reducer > LLM dialogue_act > keyword fallback。
    # 在 mode != off 的所有 v2 路径上，必须先尝试 _match_command / _match_show_more
    # 命中后短路返回，确保 /取消 /帮助 等显式命令最高优先级，不被 LLM 业务意图劫持
    # （codex review P2 防回归）。
    if mode != "off":
        cmd_result = _match_command(stripped)
        if cmd_result is not None:
            cmd, args = cmd_result
            data: dict = {"command": cmd}
            if args:
                data["args"] = args
            ir = IntentResult(intent="command", structured_data=data, confidence=1.0)
            return DialogueRouteResult(intent_result=ir, decision=None, source="legacy")
        if _match_show_more(stripped):
            ir = IntentResult(intent="show_more", confidence=1.0)
            return DialogueRouteResult(intent_result=ir, decision=None, source="legacy")

    # off：legacy 直通
    if mode == "off":
        ir = _classify_intent_legacy(
            text=stripped, role=role, history=history,
            current_criteria=current_criteria,
            user_msg_id=user_msg_id, session_hint=session_hint,
        )
        return DialogueRouteResult(intent_result=ir, decision=None, source="legacy")

    # shadow：legacy 主路由 + 按采样率旁路 v2
    if mode == "shadow":
        ir = _classify_intent_legacy(
            text=stripped, role=role, history=history,
            current_criteria=current_criteria,
            user_msg_id=user_msg_id, session_hint=session_hint,
        )
        sample_rate = getattr(settings, "dialogue_v2_shadow_sample_rate", 0.0) or 0.0
        if sample_rate > 0 and _random.random() < sample_rate and session is not None:
            try:
                extractor = get_intent_extractor()
                parse = extractor.extract_dialogue(
                    text=stripped, role=role, history=history,
                    current_criteria=current_criteria,
                    session_hint=session_hint,
                )
                # phased-plan §2.3 三类事件之一：先发 parse 再发 diff，解耦观测维度
                _emit_dialogue_v2_parse(parse, user_msg_id=user_msg_id, mode="shadow")
                decision = _reduce(parse, session, role, raw_text=stripped)
                log_event(
                    "dialogue_v2_legacy_diff",
                    user_msg_id=user_msg_id,
                    legacy_intent=ir.intent,
                    dialogue_act=decision.dialogue_act,
                    resolved_frame=decision.resolved_frame,
                    route_intent=decision.route_intent,
                    needs_clarification=bool(decision.clarification),
                    confidence=parse.confidence,
                )
            except Exception as exc:
                log_event(
                    "dialogue_v2_parse_error",
                    user_msg_id=user_msg_id,
                    error=str(exc)[:200],
                    error_type=type(exc).__name__,
                )
        return DialogueRouteResult(intent_result=ir, decision=None, source="legacy")

    # dual_read：命中目标用户走 v2，未命中走 legacy
    if mode == "dual_read" and session is not None and _is_dual_read_target(userid or ""):
        try:
            extractor = get_intent_extractor()
            parse = extractor.extract_dialogue(
                text=stripped, role=role, history=history,
                current_criteria=current_criteria,
                session_hint=session_hint,
            )
            # phased-plan §2.3 三类事件之一：先发 parse 再发 decision，解耦观测维度
            _emit_dialogue_v2_parse(parse, user_msg_id=user_msg_id, mode="dual_read")
            decision = _reduce(parse, session, role, raw_text=stripped)
            ir = decision_to_intent_result(decision, session)
            log_event(
                "dialogue_v2_decision",
                user_msg_id=user_msg_id,
                dialogue_act=decision.dialogue_act,
                resolved_frame=decision.resolved_frame,
                route_intent=decision.route_intent,
                state_transition=decision.state_transition,
                needs_clarification=bool(decision.clarification),
                confidence=parse.confidence,
            )
            return DialogueRouteResult(
                intent_result=ir, decision=decision, source="v2_dual_read",
            )
        except Exception as exc:
            log_event(
                "dialogue_v2_fallback_to_legacy",
                user_msg_id=user_msg_id,
                error=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            ir = _classify_intent_legacy(
                text=stripped, role=role, history=history,
                current_criteria=current_criteria,
                user_msg_id=user_msg_id, session_hint=session_hint,
            )
            return DialogueRouteResult(
                intent_result=ir, decision=None, source="v2_fallback_legacy",
            )

    # dual_read 未命中 / 非法 mode：legacy
    ir = _classify_intent_legacy(
        text=stripped, role=role, history=history,
        current_criteria=current_criteria,
        user_msg_id=user_msg_id, session_hint=session_hint,
    )
    return DialogueRouteResult(intent_result=ir, decision=None, source="legacy")


# ---------------------------------------------------------------------------
# 阶段三 P1+P2：用 slot_schema 覆盖字段权威清单 globals
#
# 必须在 _normalize_city_value / _normalize_job_category_value 等 schema 依赖
# 的归一化函数都已定义后再执行（schema build 时通过 lazy import 引用本模块）。
# ---------------------------------------------------------------------------


def _bootstrap_field_constants_from_schema() -> None:
    """schema 是字段权威清单的真源；本函数在 import 末尾把 globals 覆盖。

    这样：
    - _ALL_VALID_KEYS 包含 schema 中的 display 占位（如 job_title），不再被
      _normalize_structured_data 第 ~734 行的过滤器 silently drop（P1）；
    - 修改 schema 立刻反映到运行时（_VALID_JOB_KEYS / _LIST_FIELDS 等）（P2）；
    - _SEARCH_FIELD_REMAP 与 schema synonyms_in 同源。
    """
    from app.dialogue import slot_schema as _ss
    g = globals()
    g["_VALID_JOB_KEYS"] = _ss.fields_for("job_upload")
    g["_VALID_RESUME_KEYS"] = _ss.fields_for("resume_upload")
    g["_ALL_VALID_KEYS"] = _ss.all_valid_fields()
    g["_LIST_FIELDS"] = _ss.list_fields()
    g["_INT_FIELDS"] = _ss.int_fields()
    g["_SEARCH_FIELD_REMAP"] = _ss.search_synonyms()


_bootstrap_field_constants_from_schema()
