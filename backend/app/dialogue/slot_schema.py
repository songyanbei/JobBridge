"""阶段三：统一 Slot Schema（dialogue-intent-extraction-phased-plan §3）。

设计原则：

1. **元数据注册表，不是新逻辑层**：归一化函数体仍留在 intent_service.py，
   schema 通过 ``Callable`` / ``int_range`` / ``enum_values`` 字段引用，
   不做大规模代码搬家（避免阶段三引入回归）。
2. **Frame-flat 而非 slot-shared**：每个 frame 独立列出自己的 slots，
   可读性优先（同一字段 ``salary_ceiling_monthly`` 在 ``job_search`` 是
   ``soft`` 但在 ``candidate_search`` 是 ``hard``，两套独立 SlotDef）。
3. **Helper 调用方零改动**：``_legacy_required / _legacy_valid_fields /
   _legacy_compute_missing`` 三个 helper 的外部签名不变（在 intent_service.py
   中内部改为调本模块），其它模块的引用不动。
4. **Synonyms 收编**：``_SEARCH_FIELD_REMAP`` 升级为 frame.synonyms_in，
   reducer 入口先 remap 再校验。

阶段四 primary 上线前必须先完成本收口；否则 primary 模式下硬编码字段清单
仍会与 schema-driven prompt 渲染 drift。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 类型定义
# ---------------------------------------------------------------------------


PyType = Literal["str", "int", "list[str]", "bool"]
FilterMode = Literal["hard", "soft", "display"]
DefaultMerge = Literal["replace", "add", "clarify"]


@dataclass(frozen=True)
class SlotType:
    """字段的类型描述（无业务约束）。"""

    py_type: PyType
    enum_values: tuple[str, ...] | None = None  # str/enum 闭集
    int_range: tuple[int, int] | None = None   # int 字段 [lo, hi]
    # 单值归一函数；list 字段的 normalizer 作用于元素而非整 list
    normalizer: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class SlotDef:
    """字段在某 frame 内的语义（业务约束）。"""

    name: str
    slot_type: SlotType
    askable: bool
    default_merge: DefaultMerge
    filter_mode: FilterMode  # search frame 才有意义；upload frame 默认 'hard' 占位
    ranking_weight: float | None  # 阶段三全部 None；Phase 5 软偏好排序使用
    display_name: str  # 中文展示名（替代 upload_service._FIELD_DISPLAY_NAMES）
    prompt_template: str  # 追问文案模板，含 {field_display} 占位


FrameName = Literal["job_search", "candidate_search", "job_upload", "resume_upload"]


@dataclass(frozen=True)
class FrameDef:
    """frame 顶层元数据。"""

    name: FrameName
    slots: dict[str, SlotDef]
    required_all: frozenset[str]  # 全部必填
    required_any: frozenset[str]  # 任一即可（candidate_search 用）
    roles_allowed: frozenset[str]
    synonyms_in: dict[str, str] = field(default_factory=dict)  # alias -> canonical


# ---------------------------------------------------------------------------
# 引入既有归一化函数 / 常量（不重复定义，避免与 intent_service drift）
# ---------------------------------------------------------------------------


def _ref_normalize_city():
    from app.services import intent_service as _is
    return _is._normalize_city_value


def _ref_normalize_job_category():
    from app.services import intent_service as _is
    return _is._normalize_job_category_value


# ---------------------------------------------------------------------------
# 真源字段集合：阶段四前 schema 是字段权威清单，intent_service 反过来从这里拿
# （之前 schema 反向 import _VALID_JOB_KEYS 导致依赖反向、修 schema 不影响运行时）。
# ---------------------------------------------------------------------------

# job_upload frame 合法字段集合（含 hard 必填 + 上传可选字段）
_JOB_UPLOAD_FIELDS: frozenset[str] = frozenset({
    "city", "job_category", "salary_floor_monthly", "pay_type", "headcount",
    "gender_required", "is_long_term", "district", "salary_ceiling_monthly",
    "provide_meal", "provide_housing", "dorm_condition", "shift_pattern",
    "work_hours", "accept_couple", "accept_student", "accept_minority",
    "height_required", "experience_required", "education_required",
    "rebate", "employment_type", "contract_type", "min_duration",
    "job_sub_category", "age_min", "age_max",
})

# resume_upload frame 合法字段集合
_RESUME_UPLOAD_FIELDS: frozenset[str] = frozenset({
    "expected_cities", "expected_job_categories", "salary_expect_floor_monthly",
    "gender", "age", "accept_long_term", "accept_short_term",
    "expected_districts", "height", "weight", "education", "work_experience",
    "accept_night_shift", "accept_standing_work", "accept_overtime",
    "accept_outside_province", "couple_seeking_together",
    "has_health_certificate", "ethnicity", "available_from",
    "has_tattoo", "taboo",
})

# 搜索/follow_up 上 expected_* 兜底重映射（与 frame.synonyms_in 一致）
_SEARCH_FIELD_REMAP_CANONICAL: dict[str, str] = {
    "expected_cities": "city",
    "expected_job_categories": "job_category",
}


def _ref_constants() -> dict[str, Any]:
    from app.services import intent_service as _is
    from app.llm import prompts as _p
    return {
        "JOB_CATEGORY_CANONICAL": _is._JOB_CATEGORY_CANONICAL,
        "SALARY_MIN": _is._SALARY_MIN,
        "SALARY_MAX": _is._SALARY_MAX,
        "AGE_MIN": _is._AGE_MIN,
        "AGE_MAX": _is._AGE_MAX,
        "HEADCOUNT_MAX": _is._HEADCOUNT_MAX,
        "JOB_REQUIRED_FIELDS": _p.JOB_REQUIRED_FIELDS,
        "RESUME_REQUIRED_FIELDS": _p.RESUME_REQUIRED_FIELDS,
        "SEARCH_JOB_MIN_FIELDS": _p.SEARCH_JOB_MIN_FIELDS,
    }


# ---------------------------------------------------------------------------
# Slot 工厂（按字段组装 SlotType / SlotDef，避免重复字面量）
# ---------------------------------------------------------------------------


def _city_type() -> SlotType:
    return SlotType(py_type="list[str]", normalizer=_ref_normalize_city())


def _job_category_type() -> SlotType:
    c = _ref_constants()
    return SlotType(
        py_type="list[str]",
        enum_values=tuple(sorted(c["JOB_CATEGORY_CANONICAL"])),
        normalizer=_ref_normalize_job_category(),
    )


def _salary_type() -> SlotType:
    c = _ref_constants()
    return SlotType(py_type="int", int_range=(c["SALARY_MIN"], c["SALARY_MAX"]))


def _age_type() -> SlotType:
    c = _ref_constants()
    return SlotType(py_type="int", int_range=(c["AGE_MIN"], c["AGE_MAX"]))


def _headcount_type() -> SlotType:
    c = _ref_constants()
    return SlotType(py_type="int", int_range=(1, c["HEADCOUNT_MAX"]))


def _enum_str(values: tuple[str, ...]) -> SlotType:
    return SlotType(py_type="str", enum_values=values)


def _bool_type() -> SlotType:
    return SlotType(py_type="bool")


def _str_type() -> SlotType:
    return SlotType(py_type="str")


# 阶段三 P2 收尾：每个 frame 默认追问模板（context 风格），slot.prompt_template
# 真正驱动 render_missing_followup 的单字段路径；Slot-level override 立即生效。
SEARCH_PROMPT_DEFAULT = "信息还不够完整，请补充：{field_display}。"
UPLOAD_PROMPT_DEFAULT = "还需要您补充一下：{field_display}，方便我帮您处理。"


def _slot(
    name: str,
    *,
    slot_type: SlotType,
    display_name: str,
    askable: bool = False,
    default_merge: DefaultMerge = "replace",
    filter_mode: FilterMode = "soft",
    ranking_weight: float | None = None,
    prompt_template: str | None = None,
    default_template_for: Literal["search", "upload"] = "upload",
) -> SlotDef:
    if prompt_template is None:
        prompt_template = (
            SEARCH_PROMPT_DEFAULT
            if default_template_for == "search"
            else UPLOAD_PROMPT_DEFAULT
        )
    return SlotDef(
        name=name,
        slot_type=slot_type,
        askable=askable,
        default_merge=default_merge,
        filter_mode=filter_mode,
        ranking_weight=ranking_weight,
        display_name=display_name,
        prompt_template=prompt_template,
    )


def _search_slot(name: str, **kw) -> SlotDef:
    """search frame 默认走「信息还不够完整...」UX 风格的 prompt_template。"""
    kw.setdefault("default_template_for", "search")
    return _slot(name, **kw)


def _with_template(sd: SlotDef, prompt_template: str) -> SlotDef:
    """返回一个仅替换了 prompt_template 的 SlotDef 副本（dataclass frozen 拷贝）。"""
    return SlotDef(
        name=sd.name,
        slot_type=sd.slot_type,
        askable=sd.askable,
        default_merge=sd.default_merge,
        filter_mode=sd.filter_mode,
        ranking_weight=sd.ranking_weight,
        display_name=sd.display_name,
        prompt_template=prompt_template,
    )


def _apply_default_template(
    slots: dict[str, SlotDef], context: Literal["search", "upload"],
) -> dict[str, SlotDef]:
    """把仍然挂着 UPLOAD 默认模板的 slot 切到 context 对应的默认模板。

    为了让 search frame 的默认追问文案与既有 UX 对齐，同时保留每个 slot
    单独覆盖 prompt_template 的能力（构造时显式传 prompt_template 的 slot
    不会被替换）。
    """
    target = SEARCH_PROMPT_DEFAULT if context == "search" else UPLOAD_PROMPT_DEFAULT
    out: dict[str, SlotDef] = {}
    for n, sd in slots.items():
        if sd.prompt_template == UPLOAD_PROMPT_DEFAULT and target != UPLOAD_PROMPT_DEFAULT:
            out[n] = _with_template(sd, target)
        else:
            out[n] = sd
    return out


# ---------------------------------------------------------------------------
# Frame 注册（懒加载，避免 import-time 循环）
# ---------------------------------------------------------------------------


_FRAMES_CACHE: dict[str, FrameDef] | None = None


def _build_job_search() -> FrameDef:
    slots: dict[str, SlotDef] = {
        # hard 字段
        "city": _slot(
            "city", slot_type=_city_type(),
            display_name="工作城市",
            askable=True, default_merge="clarify", filter_mode="hard",
        ),
        "job_category": _slot(
            "job_category", slot_type=_job_category_type(),
            display_name="工种",
            askable=True, default_merge="replace", filter_mode="hard",
        ),
        "salary_floor_monthly": _slot(
            "salary_floor_monthly", slot_type=_salary_type(),
            display_name="月薪下限",
            askable=True, default_merge="replace", filter_mode="hard",
        ),
        "is_long_term": _slot(
            "is_long_term", slot_type=_bool_type(),
            display_name="是否长期",
            filter_mode="hard",
        ),
        "gender_required": _slot(
            "gender_required", slot_type=_enum_str(("男", "女", "不限")),
            display_name="性别要求",
            filter_mode="hard",
        ),
        "age": _slot(
            "age", slot_type=_age_type(),
            display_name="年龄",
            filter_mode="hard",
        ),
        # soft 字段（search_service._query_jobs 不做硬过滤）
        "salary_ceiling_monthly": _slot(
            "salary_ceiling_monthly", slot_type=_salary_type(),
            display_name="月薪上限",
        ),
        "provide_meal": _slot(
            "provide_meal", slot_type=_bool_type(), display_name="包吃",
        ),
        "provide_housing": _slot(
            "provide_housing", slot_type=_bool_type(), display_name="包住",
        ),
        "dorm_condition": _slot(
            "dorm_condition", slot_type=_str_type(), display_name="宿舍条件",
        ),
        "shift_pattern": _slot(
            "shift_pattern", slot_type=_str_type(), display_name="班次",
        ),
        "work_hours": _slot(
            "work_hours", slot_type=_str_type(), display_name="工时",
        ),
        "pay_type": _slot(
            "pay_type", slot_type=_enum_str(("月薪", "时薪", "计件")),
            display_name="计薪方式",
        ),
        "accept_couple": _slot(
            "accept_couple", slot_type=_bool_type(), display_name="可夫妻",
        ),
        "accept_student": _slot(
            "accept_student", slot_type=_bool_type(), display_name="可学生",
        ),
        "accept_minority": _slot(
            "accept_minority", slot_type=_bool_type(), display_name="可少数民族",
        ),
        "education_required": _slot(
            "education_required", slot_type=_str_type(), display_name="学历要求",
        ),
        "experience_required": _slot(
            "experience_required", slot_type=_str_type(), display_name="经验要求",
        ),
        "height_required": _slot(
            "height_required", slot_type=_str_type(), display_name="身高要求",
        ),
        "district": _slot("district", slot_type=_str_type(), display_name="区县"),
        "rebate": _slot("rebate", slot_type=_str_type(), display_name="返费"),
        "employment_type": _slot(
            "employment_type", slot_type=_str_type(), display_name="用工类型",
        ),
        "contract_type": _slot(
            "contract_type", slot_type=_str_type(), display_name="合同类型",
        ),
        "min_duration": _slot(
            "min_duration", slot_type=_str_type(), display_name="最短入职时长",
        ),
        "job_sub_category": _slot(
            "job_sub_category", slot_type=_str_type(), display_name="子工种",
        ),
        "age_min": _slot("age_min", slot_type=_age_type(), display_name="年龄下限"),
        "age_max": _slot("age_max", slot_type=_age_type(), display_name="年龄上限"),
        "headcount": _slot(
            "headcount", slot_type=_headcount_type(), display_name="招聘人数",
        ),
        # display 占位（phased-plan §3.1.4）
        "job_title": _slot(
            "job_title", slot_type=_str_type(),
            display_name="岗位名", filter_mode="display",
        ),
    }
    return FrameDef(
        name="job_search",
        slots=_apply_default_template(slots, "search"),
        required_all=frozenset({"city", "job_category"}),
        required_any=frozenset(),
        roles_allowed=frozenset({"worker", "broker"}),
        synonyms_in={
            "expected_cities": "city",
            "expected_job_categories": "job_category",
        },
    )


def _build_candidate_search() -> FrameDef:
    slots: dict[str, SlotDef] = {
        "city": _slot(
            "city", slot_type=_city_type(),
            display_name="工作城市",
            askable=True, default_merge="clarify", filter_mode="hard",
        ),
        "job_category": _slot(
            "job_category", slot_type=_job_category_type(),
            display_name="工种",
            askable=True, default_merge="replace", filter_mode="hard",
        ),
        "salary_ceiling_monthly": _slot(
            "salary_ceiling_monthly", slot_type=_salary_type(),
            display_name="月薪上限", filter_mode="hard",
        ),
        "gender": _slot(
            "gender", slot_type=_enum_str(("男", "女")),
            display_name="性别", filter_mode="hard",
        ),
        "age": _slot("age", slot_type=_age_type(), display_name="年龄", filter_mode="hard"),
    }
    return FrameDef(
        name="candidate_search",
        slots=_apply_default_template(slots, "search"),
        required_all=frozenset(),
        required_any=frozenset({"city", "job_category"}),
        roles_allowed=frozenset({"factory", "broker"}),
        synonyms_in={
            "expected_cities": "city",
            "expected_job_categories": "job_category",
        },
    )


def _build_job_upload() -> FrameDef:
    """job_upload 字段集来自 schema 内置的 _JOB_UPLOAD_FIELDS（schema 是真源）。"""
    c = _ref_constants()
    valid: frozenset[str] = _JOB_UPLOAD_FIELDS
    required: frozenset[str] = c["JOB_REQUIRED_FIELDS"]

    # 复用 job_search 中已经定义好的 SlotDef，再补齐 upload 独有的 askable。
    # job_search 的 prompt_template 是 search 风格；upload 场景统一切回 upload 默认，
    # 单字段追问入口 render_missing_followup 会按 slot.prompt_template 逐 slot 生效。
    job_search = _build_job_search()
    slots: dict[str, SlotDef] = {}
    for name in valid:
        sd = job_search.slots.get(name)
        if sd is None:
            # 未在 job_search 显式定义的字段（如某些 upload 专属字段），用 str fallback
            sd = _slot(name, slot_type=_str_type(), display_name=name)
        # upload 必填字段标 askable=True；filter_mode 对 upload 无意义，统一 hard。
        # 模板 search → upload 切换：保留任何 slot 自定义模板（非 SEARCH 默认）。
        if sd.prompt_template == SEARCH_PROMPT_DEFAULT:
            tmpl = UPLOAD_PROMPT_DEFAULT
        else:
            tmpl = sd.prompt_template
        slots[name] = SlotDef(
            name=sd.name,
            slot_type=sd.slot_type,
            askable=name in required,
            default_merge=sd.default_merge,
            filter_mode="hard",
            ranking_weight=None,
            display_name=sd.display_name,
            prompt_template=tmpl,
        )
    return FrameDef(
        name="job_upload",
        slots=slots,
        required_all=required,
        required_any=frozenset(),
        roles_allowed=frozenset({"factory", "broker"}),
    )


def _build_resume_upload() -> FrameDef:
    c = _ref_constants()
    valid: frozenset[str] = _RESUME_UPLOAD_FIELDS
    required: frozenset[str] = c["RESUME_REQUIRED_FIELDS"]

    # resume 字段大多是 str/list；按字段名打分类型
    list_str_fields = {
        "expected_cities", "expected_job_categories", "expected_districts",
    }
    # height / weight 当前 intent_service 不做 int 归一（用户可能输入「175cm」），
    # 保持 str 类型；阶段三的 schema 派生 _INT_FIELDS 也不应包含它们，避免
    # _normalize_structured_data 行为漂移。
    int_fields = {"salary_expect_floor_monthly", "age"}
    bool_fields = {
        "accept_long_term", "accept_short_term", "accept_night_shift",
        "accept_standing_work", "accept_overtime", "accept_outside_province",
        "couple_seeking_together", "has_health_certificate", "has_tattoo",
    }

    display_name_map = {
        "expected_cities": "期望城市",
        "expected_job_categories": "期望工种",
        "expected_districts": "期望区县",
        "salary_expect_floor_monthly": "期望月薪",
        "gender": "性别",
        "age": "年龄",
        "height": "身高",
        "weight": "体重",
        "education": "学历",
        "work_experience": "工作经验",
        "ethnicity": "民族",
        "available_from": "可入职时间",
        "taboo": "忌讳",
    }

    slots: dict[str, SlotDef] = {}
    for name in valid:
        if name in list_str_fields:
            st: SlotType
            if name == "expected_cities":
                st = _city_type()
            elif name == "expected_job_categories":
                st = _job_category_type()
            else:
                st = SlotType(py_type="list[str]")
        elif name == "salary_expect_floor_monthly":
            st = _salary_type()
        elif name == "age":
            st = _age_type()
        elif name == "gender":
            st = _enum_str(("男", "女"))
        elif name in int_fields:
            st = SlotType(py_type="int")
        elif name in bool_fields:
            st = _bool_type()
        else:
            st = _str_type()
        slots[name] = _slot(
            name, slot_type=st,
            display_name=display_name_map.get(name, name),
            askable=name in required,
            filter_mode="hard",
        )
    return FrameDef(
        name="resume_upload",
        slots=slots,
        required_all=required,
        required_any=frozenset(),
        roles_allowed=frozenset({"worker"}),
    )


def _frames() -> dict[str, FrameDef]:
    global _FRAMES_CACHE
    if _FRAMES_CACHE is None:
        _FRAMES_CACHE = {
            "job_search": _build_job_search(),
            "candidate_search": _build_candidate_search(),
            "job_upload": _build_job_upload(),
            "resume_upload": _build_resume_upload(),
        }
    return _FRAMES_CACHE


def _reset_cache_for_tests() -> None:
    """供测试在 mock intent_service 常量后清缓存。"""
    global _FRAMES_CACHE
    _FRAMES_CACHE = None


# ---------------------------------------------------------------------------
# 派生 API（reducer / intent_service legacy helper 的调用入口）
# ---------------------------------------------------------------------------


def get_frame(frame: str) -> FrameDef | None:
    return _frames().get(frame)


def fields_for(frame: str) -> frozenset[str]:
    """frame 的合法字段集合（替代 _legacy_valid_fields）。"""
    fd = get_frame(frame)
    if fd is None:
        return frozenset()
    return frozenset(fd.slots.keys())


def required_for(frame: str) -> tuple[frozenset[str], frozenset[str]]:
    """返回 (required_all, required_any)（替代 _legacy_required）。"""
    fd = get_frame(frame)
    if fd is None:
        return (frozenset(), frozenset())
    return (fd.required_all, fd.required_any)


def compute_missing_slots(frame: str, criteria: dict | None) -> list[str]:
    """按 (required_all, required_any) 算 missing（替代 _legacy_compute_missing）。

    - required_all 中尚未填值的字段全部进 missing；
    - required_any 整组都未填值时给一个组合占位（用元素 sorted 拼接）；
    - 「已填」语义：非 None / 非空字符串 / 非空列表 / 非空 dict（0/False 都算已填）。
    """
    criteria = criteria or {}
    required_all, required_any = required_for(frame)

    def _filled(name: str) -> bool:
        if name not in criteria:
            return False
        v = criteria[name]
        if v is None:
            return False
        if isinstance(v, (str, list, dict)) and not v:
            return False
        return True

    missing: list[str] = []
    for f in sorted(required_all):
        if not _filled(f):
            missing.append(f)
    if required_any:
        if not any(_filled(f) for f in required_any):
            missing.append("|".join(sorted(required_any)))
    return missing


def remap_synonyms(frame: str, slots_delta: dict | None) -> dict:
    """把 frame.synonyms_in 中声明的 alias key 映射到 canonical key。

    替代 intent_service._SEARCH_FIELD_REMAP 的硬编码使用。
    冲突时按现有 _normalize_structured_data 的行为：list 字段合并，
    标量保留首次。
    """
    if not slots_delta:
        return {}
    fd = get_frame(frame)
    if fd is None or not fd.synonyms_in:
        return dict(slots_delta)
    out: dict = {}
    for k, raw in slots_delta.items():
        target = fd.synonyms_in.get(k, k)
        if target in out:
            existing = out[target]
            if isinstance(existing, list) and isinstance(raw, list):
                for v in raw:
                    if v not in existing:
                        existing.append(v)
            # 否则保留首次
        else:
            out[target] = raw
    return out


def validate_slots_delta(
    frame: str, slots_delta: dict | None,
) -> tuple[dict, list[str]]:
    """按 frame 合法字段集过滤；不做归一化（归一化由调用方走 _normalize_structured_data）。

    返回 (accepted_raw, dropped_field_names)。
    """
    if not slots_delta:
        return {}, []
    valid = fields_for(frame)
    accepted: dict = {}
    dropped: list[str] = []
    for k, v in slots_delta.items():
        if k in valid:
            accepted[k] = v
        else:
            dropped.append(k)
    return accepted, dropped


def default_merge_policy(
    frame: str, slot: str, has_old_value: bool,
) -> DefaultMerge:
    """单字段默认 merge 策略（schema-declared）。

    reducer 在 LLM merge_hint=unknown 且非 list 字段（或无歧义）时调用。
    实际「list 字段歧义 + ambiguous_city_query_policy」决策仍在 reducer 里，
    本函数只返回 schema 声明的 default_merge。
    """
    fd = get_frame(frame)
    if fd is None:
        return "replace"
    sd = fd.slots.get(slot)
    if sd is None:
        return "replace"
    if not has_old_value:
        return "replace"
    return sd.default_merge


def check_role_permission(role: str, frame: str) -> bool:
    """角色权限校验（替代 _ROLE_FRAME_PERMISSIONS）。

    frame='none' 一律允许（chitchat / cancel / reset 等）。
    """
    if frame == "none":
        return True
    fd = get_frame(frame)
    if fd is None:
        return False
    return role in fd.roles_allowed


def is_list_slot(frame: str, slot: str) -> bool:
    """该 frame 内某字段是否 list 类型（reducer _resolve_merge_policy 用）。"""
    fd = get_frame(frame)
    if fd is None:
        return False
    sd = fd.slots.get(slot)
    if sd is None:
        return False
    return sd.slot_type.py_type == "list[str]"


def display_name(frame: str, slot: str) -> str | None:
    fd = get_frame(frame)
    if fd is None:
        return None
    sd = fd.slots.get(slot)
    return sd.display_name if sd else None


def all_valid_fields() -> frozenset[str]:
    """所有 frame 字段并集（替代 intent_service._ALL_VALID_KEYS）。

    包括 schema 中的 display 占位字段（如 job_title），让 LLM 抽出后能正常
    通过过滤进入 final_search_criteria（不被 silently dropped）。
    """
    out: set[str] = set()
    for fd in _frames().values():
        out.update(fd.slots.keys())
    return frozenset(out)


def list_fields() -> frozenset[str]:
    """所有 py_type='list[str]' 的字段名（替代 intent_service._LIST_FIELDS）。"""
    out: set[str] = set()
    for fd in _frames().values():
        for sd in fd.slots.values():
            if sd.slot_type.py_type == "list[str]":
                out.add(sd.name)
    # 兼容上传场景：expected_cities / expected_job_categories 在 resume_upload
    # frame 里我们建的是 list[str]，已经包括；保险起见显式加入
    out.update({"expected_cities", "expected_job_categories"})
    return frozenset(out)


def int_fields() -> frozenset[str]:
    """所有 py_type='int' 的字段名（替代 intent_service._INT_FIELDS）。"""
    out: set[str] = set()
    for fd in _frames().values():
        for sd in fd.slots.values():
            if sd.slot_type.py_type == "int":
                out.add(sd.name)
    return frozenset(out)


def search_synonyms() -> dict[str, str]:
    """搜索 frame 共享的 alias→canonical 字段映射（替代 _SEARCH_FIELD_REMAP）。

    用于 _normalize_structured_data 中对搜索 intent 做兜底重映射。
    """
    return dict(_SEARCH_FIELD_REMAP_CANONICAL)


def key_fields_for_low_confidence() -> frozenset[str]:
    """低置信度兜底关心的关键字段集合：所有 search frame 中 ``filter_mode=hard``
    的「业务关键字段」。

    与阶段一/二的 _KEY_FIELDS_FOR_LOW_CONFIDENCE 行为对齐：包含 city /
    job_category / salary_floor_monthly / salary_ceiling_monthly，
    其它硬字段（is_long_term / gender / age）由 LLM 高置信度抽取，不参与兜底。
    """
    out: set[str] = set()
    for name in ("job_search", "candidate_search"):
        fd = get_frame(name)
        if fd is None:
            continue
        for sd in fd.slots.values():
            if sd.filter_mode != "hard":
                continue
            # 业务关键字段：city / job_category 列表字段，或薪资 int 字段
            if sd.name in {"city", "job_category"}:
                out.add(sd.name)
            elif sd.name.startswith("salary_"):
                out.add(sd.name)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Clarification 模板渲染
# ---------------------------------------------------------------------------


_CLARIFICATION_TEMPLATES: dict[str, str] = {
    "city_replace_or_add": "您是想换成{new_value}，还是{old_value}和{new_value}都看？",
    # 单字段：搜索追问场景
    "missing_required_slot": "信息还不够完整，请补充：{field_display}。",
    # 多字段：搜索追问场景，1-2 字段 inline、3+ 字段列表式
    "missing_required_slots_inline": "信息还不够完整，请补充：{field_displays_joined}。",
    "missing_required_slots_list": "信息还不够完整，请补充：\n{field_displays_listed}",
    # 上传草稿追问场景（与搜索的措辞略不同：用户已在上传上下文里）
    "missing_upload_slot": "还需要您补充一下：{field_display}，方便我帮您处理。",
    "missing_upload_slots_inline": "还需要您补充一下：{field_displays_joined}，方便我帮您处理。",
    "missing_upload_slots_list": "还缺少以下信息，请补充：\n{field_displays_listed}",
    "frame_conflict": "当前正在处理上传草稿，是先继续发布、取消草稿，还是先做新的事？",
    "low_confidence": "我不太确定您的意思，能具体说一下吗？",
    "role_no_permission": "您的角色当前不支持这个操作。",
}


def render_missing_followup(
    missing: list[str],
    frame: str | None,
    *,
    context: Literal["search", "upload"] = "search",
    fallback_display: dict[str, str] | None = None,
) -> str:
    """搜索/上传缺字段统一追问文案（schema-driven，slot-level template 真正生效）。

    - **1 字段**：优先用 ``frame.slots[slot].prompt_template``（slot 级 override
      立即生效，例：把 ``salary_floor_monthly.prompt_template`` 改成
      「您接受的最低月薪是多少？」就能直接驱动追问文案）；schema 缺失时回退
      到全局模板 ``missing_{search,upload}_slot``。
    - **2 字段**：``missing_{search,upload}_slots_inline``，搜索全宽顿号 / 上传「和」分隔
    - **3+ 字段**：``missing_{search,upload}_slots_list``，前缀 ``- `` 列表式

    ``fallback_display`` 在 schema 没有 display_name 时兜底（兼容旧
    ``_FIELD_DISPLAY_NAMES``）。
    """
    if not missing:
        return ""
    fallback_display = fallback_display or {}
    fd = get_frame(frame) if frame else None
    names: list[str] = []
    for f in missing:
        n = display_name(frame, f) if frame else None
        names.append(n or fallback_display.get(f) or f)

    prefix = "missing_required" if context == "search" else "missing_upload"
    if len(names) == 1:
        slot_name = missing[0]
        # 优先用 slot.prompt_template（slot-level override）
        if fd is not None:
            sd = fd.slots.get(slot_name)
            if sd is not None and sd.prompt_template:
                try:
                    return sd.prompt_template.format(field_display=names[0])
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "slot_schema: slot %s.%s prompt_template render failed: %s",
                        frame, slot_name, exc,
                    )
        return render_clarification(
            f"{prefix}_slot", frame, slot_name,
            extra={"field_display": names[0]},
        )
    if len(names) == 2:
        # 上传场景沿用原 UX 的「和」分隔，搜索场景用顿号「、」
        sep = "和" if context == "upload" else "、"
        joined = sep.join(names)
        return render_clarification(
            f"{prefix}_slots_inline", frame,
            extra={"field_displays_joined": joined},
        )
    listed = "\n".join(f"- {n}" for n in names)
    return render_clarification(
        f"{prefix}_slots_list", frame,
        extra={"field_displays_listed": listed},
    )


def render_clarification(
    kind: str,
    frame: str | None = None,
    slot: str | None = None,
    *,
    options: list | None = None,
    extra: dict | None = None,
) -> str:
    """按 kind 渲染结构化反问文案。模板缺失或渲染失败时返回 fallback 字符串。

    extra 用于 city_replace_or_add 这类需要带 old_value/new_value 的场景。
    """
    template = _CLARIFICATION_TEMPLATES.get(kind)
    if not template:
        return "请补充更多信息。"
    field_display = ""
    if frame and slot:
        field_display = display_name(frame, slot) or slot
    ctx: dict[str, Any] = {
        "field_display": field_display,
        "old_value": "",
        "new_value": "",
    }
    if extra:
        for k, v in extra.items():
            if isinstance(v, list):
                ctx[k] = "、".join(str(x) for x in v)
            else:
                ctx[k] = str(v)
    try:
        return template.format(**ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("slot_schema: clarification render failed kind=%s err=%s", kind, exc)
        return "请补充更多信息。"


# ---------------------------------------------------------------------------
# Prompt 字段清单渲染（DIALOGUE_PARSE_PROMPT_V2 用）
# ---------------------------------------------------------------------------


def render_prompt_field_spec() -> str:
    """生成 prompt 中 frame → 字段清单段落。

    输出格式（每个 frame 一段）：

        ### job_search 可输出字段
        - city (list[str])：工作城市
        - job_category (list[str], enum=[餐饮,...])：工种
        - salary_floor_monthly (int, 500-200000)：月薪下限
        ...

    阶段三 prompt 不再硬编码字段清单，启动期一次性渲染常量字符串。
    """
    lines: list[str] = []
    for frame_name in ("job_search", "candidate_search", "job_upload", "resume_upload"):
        fd = get_frame(frame_name)
        if fd is None:
            continue
        lines.append(f"### {frame_name} 可输出字段")
        for slot_name in sorted(fd.slots.keys()):
            sd = fd.slots[slot_name]
            type_desc = _format_type_desc(sd.slot_type)
            required_marker = ""
            if slot_name in fd.required_all:
                required_marker = "（必填）"
            elif slot_name in fd.required_any:
                required_marker = "（必填: 任一）"
            lines.append(
                f"- {slot_name} ({type_desc}){required_marker}：{sd.display_name}"
            )
        if fd.synonyms_in:
            syn_lines = [f"  - {a} → {c}" for a, c in fd.synonyms_in.items()]
            lines.append("  同义字段（自动归一）：")
            lines.extend(syn_lines)
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_type_desc(st: SlotType) -> str:
    parts: list[str] = [st.py_type]
    if st.enum_values:
        # 枚举太长就截断（job_category 10 个值）
        vs = ",".join(st.enum_values[:12])
        suffix = "..." if len(st.enum_values) > 12 else ""
        parts.append(f"enum=[{vs}{suffix}]")
    if st.int_range:
        parts.append(f"{st.int_range[0]}-{st.int_range[1]}")
    return ", ".join(parts)
