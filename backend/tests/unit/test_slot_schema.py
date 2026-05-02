"""阶段三 slot_schema 单测（dialogue-intent-extraction-phased-plan §3.4）。

覆盖：
- 4 个 frame 的 fields_for / required_for / compute_missing_slots
- normalizer 边界值（city / job_category / int range / list）
- 角色权限矩阵
- synonyms_in remap
- list 类型识别
- prompt 渲染包含必要片段
- clarification 模板
"""
from __future__ import annotations

import pytest

from app.dialogue import slot_schema


# ---------------------------------------------------------------------------
# fields_for / required_for / compute_missing_slots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame,must_contain",
    [
        ("job_search", {"city", "job_category", "salary_floor_monthly", "is_long_term"}),
        ("candidate_search", {"city", "job_category", "salary_ceiling_monthly", "gender", "age"}),
        ("job_upload", {"city", "job_category", "pay_type", "headcount"}),
        ("resume_upload", {"expected_cities", "expected_job_categories", "salary_expect_floor_monthly"}),
    ],
)
def test_fields_for_contains_expected_keys(frame, must_contain):
    fields = slot_schema.fields_for(frame)
    missing = must_contain - fields
    assert not missing, f"frame={frame} missing fields: {missing}"


def test_fields_for_unknown_frame():
    assert slot_schema.fields_for("nonsense") == frozenset()


@pytest.mark.parametrize(
    "frame,expected_all,expected_any",
    [
        ("job_search", frozenset({"city", "job_category"}), frozenset()),
        ("candidate_search", frozenset(), frozenset({"city", "job_category"})),
    ],
)
def test_required_for_search_frames(frame, expected_all, expected_any):
    req_all, req_any = slot_schema.required_for(frame)
    assert req_all == expected_all
    assert req_any == expected_any


def test_required_for_job_upload_matches_prompts_constant():
    from app.llm.prompts import JOB_REQUIRED_FIELDS

    req_all, req_any = slot_schema.required_for("job_upload")
    assert req_all == JOB_REQUIRED_FIELDS
    assert req_any == frozenset()


def test_compute_missing_slots_job_search_empty():
    assert slot_schema.compute_missing_slots("job_search", {}) == ["city", "job_category"]


def test_compute_missing_slots_job_search_partial():
    assert slot_schema.compute_missing_slots(
        "job_search", {"city": ["北京市"]},
    ) == ["job_category"]


def test_compute_missing_slots_job_search_full():
    assert slot_schema.compute_missing_slots(
        "job_search", {"city": ["北京市"], "job_category": ["餐饮"]},
    ) == []


def test_compute_missing_slots_candidate_search_empty_uses_any_placeholder():
    # required_any: 整组都缺时用 "|" 拼占位
    out = slot_schema.compute_missing_slots("candidate_search", {})
    assert out == ["city|job_category"]


def test_compute_missing_slots_candidate_search_any_satisfied():
    assert slot_schema.compute_missing_slots(
        "candidate_search", {"city": ["北京市"]},
    ) == []
    assert slot_schema.compute_missing_slots(
        "candidate_search", {"job_category": ["餐饮"]},
    ) == []


@pytest.mark.parametrize("filled_value", ["北京市", ["北京市"], 0, False, {"k": "v"}])
def test_compute_missing_slots_filled_semantics(filled_value):
    """0 / False 算已填，None / 空字符串 / 空列表 / 空 dict 不算。"""
    out = slot_schema.compute_missing_slots(
        "job_search", {"city": filled_value, "job_category": ["餐饮"]},
    )
    assert "city" not in out


@pytest.mark.parametrize("empty_value", [None, "", [], {}])
def test_compute_missing_slots_empty_values_count_as_missing(empty_value):
    out = slot_schema.compute_missing_slots(
        "job_search", {"city": empty_value, "job_category": ["餐饮"]},
    )
    assert out == ["city"]


# ---------------------------------------------------------------------------
# validate_slots_delta
# ---------------------------------------------------------------------------


def test_validate_slots_delta_drops_unknown_fields():
    accepted, dropped = slot_schema.validate_slots_delta(
        "job_search", {"city": ["北京市"], "totally_unknown": "x"},
    )
    assert "city" in accepted
    assert "totally_unknown" not in accepted
    assert "totally_unknown" in dropped


def test_validate_slots_delta_accepts_soft_fields():
    """阶段三：soft 字段（provide_meal / provide_housing / shift_pattern）必须接受。"""
    accepted, dropped = slot_schema.validate_slots_delta(
        "job_search",
        {"provide_meal": True, "provide_housing": True, "shift_pattern": "白班"},
    )
    assert set(accepted.keys()) == {"provide_meal", "provide_housing", "shift_pattern"}
    assert dropped == []


def test_validate_slots_delta_empty():
    accepted, dropped = slot_schema.validate_slots_delta("job_search", {})
    assert accepted == {}
    assert dropped == []


# ---------------------------------------------------------------------------
# remap_synonyms (吸收 _SEARCH_FIELD_REMAP)
# ---------------------------------------------------------------------------


def test_remap_synonyms_expected_cities_to_city():
    out = slot_schema.remap_synonyms(
        "job_search", {"expected_cities": ["北京市"]},
    )
    assert out == {"city": ["北京市"]}


def test_remap_synonyms_candidate_search_also_remaps():
    out = slot_schema.remap_synonyms(
        "candidate_search", {"expected_job_categories": ["餐饮"]},
    )
    assert out == {"job_category": ["餐饮"]}


def test_remap_synonyms_no_alias_passthrough():
    out = slot_schema.remap_synonyms("job_search", {"city": ["北京市"]})
    assert out == {"city": ["北京市"]}


def test_remap_synonyms_unknown_frame_returns_copy():
    inp = {"expected_cities": ["北京市"]}
    out = slot_schema.remap_synonyms("nonsense", inp)
    assert out == inp
    assert out is not inp


def test_remap_synonyms_collision_merges_lists():
    """LLM 同时给 expected_cities 与 city 时，列表合并去重。"""
    out = slot_schema.remap_synonyms(
        "job_search",
        {"city": ["北京市"], "expected_cities": ["北京市", "上海市"]},
    )
    # canonical city 先出现，expected_cities 合并入 city
    assert sorted(out["city"]) == ["上海市", "北京市"]


# ---------------------------------------------------------------------------
# default_merge_policy / is_list_slot
# ---------------------------------------------------------------------------


def test_default_merge_policy_no_old_value_is_replace():
    assert slot_schema.default_merge_policy("job_search", "city", False) == "replace"


def test_default_merge_policy_city_with_old_value_is_clarify():
    assert slot_schema.default_merge_policy("job_search", "city", True) == "clarify"


def test_default_merge_policy_salary_with_old_value_is_replace():
    assert slot_schema.default_merge_policy(
        "job_search", "salary_floor_monthly", True,
    ) == "replace"


def test_default_merge_policy_unknown_frame():
    assert slot_schema.default_merge_policy("nonsense", "city", True) == "replace"


def test_is_list_slot_city():
    assert slot_schema.is_list_slot("job_search", "city") is True
    assert slot_schema.is_list_slot("candidate_search", "city") is True


def test_is_list_slot_scalar_field():
    assert slot_schema.is_list_slot("job_search", "salary_floor_monthly") is False
    assert slot_schema.is_list_slot("job_search", "is_long_term") is False


def test_is_list_slot_unknown():
    assert slot_schema.is_list_slot("job_search", "nonsense_field") is False
    assert slot_schema.is_list_slot("nonsense_frame", "city") is False


# ---------------------------------------------------------------------------
# 角色权限
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,frame,expected",
    [
        ("worker", "job_search", True),
        ("worker", "resume_upload", True),
        ("worker", "job_upload", False),
        ("worker", "candidate_search", False),
        ("factory", "candidate_search", True),
        ("factory", "job_upload", True),
        ("factory", "job_search", False),
        ("factory", "resume_upload", False),
        ("broker", "job_search", True),
        ("broker", "candidate_search", True),
        ("broker", "job_upload", True),
        ("broker", "resume_upload", False),
        ("worker", "none", True),  # frame=none 一律允许
        ("anyone", "none", True),
    ],
)
def test_check_role_permission_matrix(role, frame, expected):
    assert slot_schema.check_role_permission(role, frame) is expected


def test_check_role_permission_unknown_frame():
    assert slot_schema.check_role_permission("worker", "nonsense") is False


# ---------------------------------------------------------------------------
# normalizer / SlotType 边界
# ---------------------------------------------------------------------------


def test_city_slot_normalizer_normalizes_short_name():
    """SlotType.normalizer 引用 intent_service._normalize_city_value。"""
    fd = slot_schema.get_frame("job_search")
    assert fd is not None
    city_slot = fd.slots["city"]
    assert city_slot.slot_type.normalizer is not None
    assert city_slot.slot_type.normalizer("北京") == "北京市"


def test_job_category_slot_normalizer_maps_synonym():
    fd = slot_schema.get_frame("job_search")
    assert fd is not None
    jc = fd.slots["job_category"]
    assert jc.slot_type.normalizer is not None
    assert jc.slot_type.normalizer("服务员") == "餐饮"


def test_salary_slot_int_range():
    fd = slot_schema.get_frame("job_search")
    assert fd is not None
    sf = fd.slots["salary_floor_monthly"]
    assert sf.slot_type.int_range == (500, 200000)


def test_age_slot_int_range():
    fd = slot_schema.get_frame("job_search")
    assert fd is not None
    age = fd.slots["age"]
    assert age.slot_type.int_range == (14, 80)


def test_pay_type_enum_values():
    fd = slot_schema.get_frame("job_search")
    assert fd is not None
    pt = fd.slots["pay_type"]
    assert pt.slot_type.enum_values == ("月薪", "时薪", "计件")


def test_job_category_enum_includes_canonical_set():
    """job_category enum 派生自 _JOB_CATEGORY_CANONICAL。"""
    fd = slot_schema.get_frame("job_search")
    assert fd is not None
    jc = fd.slots["job_category"]
    enum = set(jc.slot_type.enum_values or ())
    assert "餐饮" in enum
    assert "电子厂" in enum
    assert "其他" in enum


# ---------------------------------------------------------------------------
# filter_mode / job_title 占位
# ---------------------------------------------------------------------------


def test_provide_meal_is_soft_filter_mode():
    fd = slot_schema.get_frame("job_search")
    assert fd is not None
    assert fd.slots["provide_meal"].filter_mode == "soft"
    assert fd.slots["provide_housing"].filter_mode == "soft"
    assert fd.slots["shift_pattern"].filter_mode == "soft"


def test_city_is_hard_filter_mode_in_search_frames():
    for frame_name in ("job_search", "candidate_search"):
        fd = slot_schema.get_frame(frame_name)
        assert fd is not None
        assert fd.slots["city"].filter_mode == "hard"


def test_job_title_is_display_filter_mode():
    """phased-plan §3.1.4：job_title 占位 filter_mode=display。"""
    fd = slot_schema.get_frame("job_search")
    assert fd is not None
    assert "job_title" in fd.slots
    assert fd.slots["job_title"].filter_mode == "display"
    assert fd.slots["job_title"].ranking_weight is None
    assert fd.slots["job_title"].askable is False


def test_all_slots_have_ranking_weight_none_in_stage_3():
    """阶段三：所有 slot ranking_weight 全 None；Phase 5 才使用。"""
    for frame_name in ("job_search", "candidate_search", "job_upload", "resume_upload"):
        fd = slot_schema.get_frame(frame_name)
        assert fd is not None
        for sd in fd.slots.values():
            assert sd.ranking_weight is None, f"{frame_name}.{sd.name} should be None"


# ---------------------------------------------------------------------------
# display_name / clarification
# ---------------------------------------------------------------------------


def test_display_name_lookup():
    assert slot_schema.display_name("job_search", "city") == "工作城市"
    assert slot_schema.display_name("job_search", "salary_floor_monthly") == "月薪下限"
    assert slot_schema.display_name("job_upload", "headcount") == "招聘人数"
    assert slot_schema.display_name("nonsense", "city") is None
    assert slot_schema.display_name("job_search", "nonsense") is None


def test_render_clarification_city_replace_or_add():
    text = slot_schema.render_clarification(
        "city_replace_or_add",
        frame="job_search", slot="city",
        extra={"old_value": ["西安市"], "new_value": ["北京市"]},
    )
    assert "西安市" in text
    assert "北京市" in text


def test_render_clarification_missing_required_slot():
    text = slot_schema.render_clarification(
        "missing_required_slot", frame="job_search", slot="city",
    )
    assert "工作城市" in text


def test_render_clarification_unknown_kind_returns_fallback():
    text = slot_schema.render_clarification("nonsense_kind")
    assert text  # non-empty fallback


# ---------------------------------------------------------------------------
# prompt 渲染
# ---------------------------------------------------------------------------


def test_render_prompt_field_spec_contains_all_frames():
    spec = slot_schema.render_prompt_field_spec()
    for frame in ("job_search", "candidate_search", "job_upload", "resume_upload"):
        assert f"### {frame}" in spec


def test_render_prompt_field_spec_marks_required():
    spec = slot_schema.render_prompt_field_spec()
    # job_search 的 city 必填
    assert "（必填）" in spec
    # candidate_search 的 city / job_category 是 required_any
    assert "（必填: 任一）" in spec


def test_render_prompt_field_spec_includes_int_range():
    spec = slot_schema.render_prompt_field_spec()
    assert "500-200000" in spec  # salary range
    assert "14-80" in spec  # age range


def test_render_prompt_field_spec_includes_synonyms_section():
    spec = slot_schema.render_prompt_field_spec()
    assert "expected_cities → city" in spec
    assert "expected_job_categories → job_category" in spec


# ---------------------------------------------------------------------------
# key_fields_for_low_confidence
# ---------------------------------------------------------------------------


def test_key_fields_for_low_confidence_matches_phase2_baseline():
    """与阶段一/二 _KEY_FIELDS_FOR_LOW_CONFIDENCE 行为对齐。"""
    out = slot_schema.key_fields_for_low_confidence()
    assert out == frozenset(
        {"city", "job_category", "salary_floor_monthly", "salary_ceiling_monthly"}
    )


# ---------------------------------------------------------------------------
# legacy helper bridge（确认 intent_service 三个 helper 内部走 schema）
# ---------------------------------------------------------------------------


def test_legacy_required_delegates_to_schema():
    from app.services import intent_service as _is
    assert _is._legacy_required("job_search") == slot_schema.required_for("job_search")
    assert _is._legacy_required("candidate_search") == slot_schema.required_for("candidate_search")


def test_legacy_valid_fields_delegates_to_schema():
    from app.services import intent_service as _is
    assert _is._legacy_valid_fields("job_search") == slot_schema.fields_for("job_search")


def test_legacy_compute_missing_delegates_to_schema():
    from app.services import intent_service as _is
    assert _is._legacy_compute_missing("job_search", {}) == ["city", "job_category"]
    assert _is._legacy_compute_missing("candidate_search", {}) == ["city|job_category"]


# ---------------------------------------------------------------------------
# P1 / P2.1：intent_service 字段权威清单全部从 schema 派生
# ---------------------------------------------------------------------------


class TestIntentServiceConstantsFromSchema:
    """codex review P1+P2.1：5 个常量必须 == schema 派生结果。"""

    def test_valid_job_keys_equals_schema(self):
        from app.services import intent_service as _is
        assert _is._VALID_JOB_KEYS == slot_schema.fields_for("job_upload")

    def test_valid_resume_keys_equals_schema(self):
        from app.services import intent_service as _is
        assert _is._VALID_RESUME_KEYS == slot_schema.fields_for("resume_upload")

    def test_all_valid_keys_includes_job_title(self):
        """codex review P1：job_title 是 display 占位，必须在 _ALL_VALID_KEYS 中。"""
        from app.services import intent_service as _is
        assert "job_title" in _is._ALL_VALID_KEYS

    def test_all_valid_keys_equals_schema_union(self):
        from app.services import intent_service as _is
        assert _is._ALL_VALID_KEYS == slot_schema.all_valid_fields()

    def test_list_fields_equals_schema(self):
        from app.services import intent_service as _is
        assert _is._LIST_FIELDS == slot_schema.list_fields()

    def test_int_fields_equals_schema(self):
        from app.services import intent_service as _is
        assert _is._INT_FIELDS == slot_schema.int_fields()

    def test_search_field_remap_equals_schema(self):
        from app.services import intent_service as _is
        assert dict(_is._SEARCH_FIELD_REMAP) == slot_schema.search_synonyms()


# ---------------------------------------------------------------------------
# P1：job_title 通过 _normalize_structured_data 不被 silently dropped
# ---------------------------------------------------------------------------


class TestJobTitleNotDropped:
    def test_job_title_preserved_through_normalize_search_intent(self):
        """LLM 抽出 job_title 后 _ALL_VALID_KEYS 应放行，写进 final criteria。"""
        from app.services import intent_service as _is
        out = _is._normalize_structured_data(
            {"city": "北京市", "job_category": "餐饮", "job_title": "服务员"},
            role="worker", intent="search_job",
        )
        assert out.get("job_title") == "服务员"

    def test_job_title_preserved_through_reducer(self):
        """reducer 主路径下 job_title 不被 schema validate 过滤掉。"""
        from app.llm.base import DialogueParseResult
        from app.schemas.conversation import SessionState
        from app.services.dialogue_reducer import reduce
        s = SessionState(
            role="worker", active_flow="idle",
            search_criteria={}, awaiting_fields=[], awaiting_frame=None,
            pending_upload={}, pending_upload_intent=None,
        )
        parse = DialogueParseResult(
            dialogue_act="start_search",
            frame_hint="job_search",
            slots_delta={
                "city": ["北京市"], "job_category": ["餐饮"],
                "job_title": "服务员",
            },
            merge_hint={},
            needs_clarification=False,
            confidence=0.95,
            conflict_action=None,
        )
        d = reduce(parse, s, "worker")
        assert "job_title" in d.accepted_slots_delta
        assert d.accepted_slots_delta["job_title"] == "服务员"
        assert d.final_search_criteria.get("job_title") == "服务员"


# ---------------------------------------------------------------------------
# P2.2：reducer 消费 schema.default_merge_policy
# ---------------------------------------------------------------------------


class TestReducerConsumesDefaultMerge:
    """codex review P2：_resolve_merge_policy 改写后必须真正回读 schema。"""

    def test_schema_change_to_replace_overrides_clarify_default(self, monkeypatch):
        """把 city 的 default_merge 临时改成 replace，reducer 行为应跟随。"""
        from app.llm.base import DialogueParseResult
        from app.schemas.conversation import SessionState
        from app.services.dialogue_reducer import reduce

        # monkey-patch schema.default_merge_policy 让 city 永远返回 replace
        original = slot_schema.default_merge_policy
        def _patched(frame, slot, has_old):
            if slot == "city":
                return "replace"
            return original(frame, slot, has_old)
        monkeypatch.setattr(slot_schema, "default_merge_policy", _patched)

        s = SessionState(
            role="worker", active_flow="search_active",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
            awaiting_fields=[], awaiting_frame=None,
            pending_upload={}, pending_upload_intent=None,
        )
        parse = DialogueParseResult(
            dialogue_act="modify_search",
            frame_hint="job_search",
            slots_delta={"city": ["北京市"]},
            merge_hint={"city": "unknown"},
            needs_clarification=False,
            confidence=0.95,
            conflict_action=None,
        )
        d = reduce(parse, s, "worker")
        # schema 现在说 city → replace（无 clarify），reducer 应直接替换不反问
        assert d.clarification is None
        assert d.final_search_criteria.get("city") == ["北京市"]

    def test_schema_default_replace_for_salary_passes_through(self):
        """salary 字段 schema 默认 replace，reducer 正常 replace 不走 clarify。"""
        from app.llm.base import DialogueParseResult
        from app.schemas.conversation import SessionState
        from app.services.dialogue_reducer import reduce

        s = SessionState(
            role="worker", active_flow="search_active",
            search_criteria={
                "city": ["北京市"], "job_category": ["餐饮"],
                "salary_floor_monthly": 5000,
            },
            awaiting_fields=[], awaiting_frame=None,
            pending_upload={}, pending_upload_intent=None,
        )
        parse = DialogueParseResult(
            dialogue_act="modify_search",
            frame_hint="job_search",
            slots_delta={"salary_floor_monthly": 8000},
            merge_hint={},
            needs_clarification=False,
            confidence=0.95,
            conflict_action=None,
        )
        d = reduce(parse, s, "worker")
        assert d.clarification is None
        assert d.final_search_criteria["salary_floor_monthly"] == 8000


# ---------------------------------------------------------------------------
# P2.3：追问文案 schema-driven
# ---------------------------------------------------------------------------


class TestFollowupTextsSchemaDriven:
    def test_render_missing_followup_search_one_field(self):
        text = slot_schema.render_missing_followup(
            ["city"], "job_search", context="search",
        )
        assert "工作城市" in text
        assert "信息还不够完整" in text

    def test_render_missing_followup_search_two_fields_inline(self):
        text = slot_schema.render_missing_followup(
            ["city", "job_category"], "job_search", context="search",
        )
        assert "工作城市" in text
        assert "工种" in text
        # 搜索场景用顿号
        assert "、" in text

    def test_render_missing_followup_search_three_fields_listed(self):
        text = slot_schema.render_missing_followup(
            ["city", "job_category", "salary_floor_monthly"],
            "job_search", context="search",
        )
        assert "- 工作城市" in text
        assert "- 工种" in text
        assert "- 月薪下限" in text

    def test_render_missing_followup_upload_uses_he_separator(self):
        """上传场景沿用「和」分隔（与原 _generate_followup_text 一致）。"""
        text = slot_schema.render_missing_followup(
            ["city", "job_category"], "job_upload", context="upload",
        )
        assert "和" in text
        assert "方便我帮您处理" in text

    def test_render_missing_followup_falls_back_to_display_dict(self):
        """schema 没有该字段时回退 fallback_display 字典。"""
        text = slot_schema.render_missing_followup(
            ["nonsense_field"], "job_search", context="search",
            fallback_display={"nonsense_field": "未知项"},
        )
        assert "未知项" in text

    def test_message_router_missing_follow_up_uses_schema(self):
        """search 追问入口实际调用 schema 模板（不再硬编码格式串）。"""
        from app.services.message_router import _missing_follow_up_text
        text = _missing_follow_up_text(["city", "job_category"], frame="job_search")
        assert "工作城市" in text and "工种" in text and "、" in text

    def test_upload_service_followup_uses_schema(self):
        from app.services.upload_service import _generate_followup_text
        text = _generate_followup_text(["city", "job_category"])
        assert "工作城市" in text and "工种" in text and "和" in text


# ---------------------------------------------------------------------------
# P2 收尾：SlotDef.prompt_template 真正驱动单字段追问（非死元数据）
# ---------------------------------------------------------------------------


class TestSlotPromptTemplateDrives:
    """codex review P2 收尾：改 slot.prompt_template 必须直接影响真实追问文案。"""

    def test_search_slot_default_template_matches_search_style(self):
        """job_search frame 的默认 askable slot prompt_template = SEARCH 风格。"""
        fd = slot_schema.get_frame("job_search")
        assert fd is not None
        # askable 字段（city / job_category / salary_floor_monthly）应继承 SEARCH 默认
        for name in ("city", "job_category", "salary_floor_monthly"):
            assert (
                fd.slots[name].prompt_template == slot_schema.SEARCH_PROMPT_DEFAULT
            ), f"{name} prompt_template should default to SEARCH style"

    def test_upload_slot_default_template_matches_upload_style(self):
        fd = slot_schema.get_frame("job_upload")
        assert fd is not None
        for name in ("city", "job_category", "salary_floor_monthly", "headcount", "pay_type"):
            assert (
                fd.slots[name].prompt_template == slot_schema.UPLOAD_PROMPT_DEFAULT
            ), f"{name} prompt_template should default to UPLOAD style"

    def test_render_missing_followup_consumes_slot_prompt_template(self, monkeypatch):
        """改 slot.prompt_template 后单字段追问文案立刻跟随。"""
        # 临时把 job_search.salary_floor_monthly 的 prompt_template 改成自定义
        custom = "您接受的最低月薪是多少？（参考：{field_display}）"
        original_frames = slot_schema._frames()
        original_slot = original_frames["job_search"].slots["salary_floor_monthly"]

        # 构造新的 SlotDef 副本
        new_slot = slot_schema._with_template(original_slot, custom)
        # 直接 patch frames cache 的对应字段
        original_frames["job_search"].slots["salary_floor_monthly"] = new_slot
        try:
            text = slot_schema.render_missing_followup(
                ["salary_floor_monthly"], "job_search", context="search",
            )
            assert "您接受的最低月薪是多少？" in text
            assert "月薪下限" in text  # field_display 替换生效
        finally:
            # 恢复，避免污染其它测试
            original_frames["job_search"].slots["salary_floor_monthly"] = original_slot

    def test_render_missing_followup_falls_back_when_slot_template_render_fails(
        self, monkeypatch,
    ):
        """slot.prompt_template 渲染失败应回退到全局 missing_required_slot 模板。"""
        original_frames = slot_schema._frames()
        original_slot = original_frames["job_search"].slots["city"]
        # 故意制造一个会 KeyError 的模板
        bad = "请补充：{nonexistent_placeholder}"
        original_frames["job_search"].slots["city"] = slot_schema._with_template(
            original_slot, bad,
        )
        try:
            text = slot_schema.render_missing_followup(
                ["city"], "job_search", context="search",
            )
            # 回退后应包含 SEARCH 全局默认的「信息还不够完整」+ 工作城市
            assert "信息还不够完整" in text
            assert "工作城市" in text
        finally:
            original_frames["job_search"].slots["city"] = original_slot

    def test_render_missing_followup_search_uses_slot_template_via_message_router(
        self, monkeypatch,
    ):
        """改 slot.prompt_template 后，message_router 入口的文案也跟随。"""
        from app.services.message_router import _missing_follow_up_text

        original_frames = slot_schema._frames()
        original_slot = original_frames["job_search"].slots["job_category"]
        custom = "您想找哪一类工作？（系统默认问的字段：{field_display}）"
        original_frames["job_search"].slots["job_category"] = slot_schema._with_template(
            original_slot, custom,
        )
        try:
            text = _missing_follow_up_text(["job_category"], frame="job_search")
            assert "您想找哪一类工作？" in text
        finally:
            original_frames["job_search"].slots["job_category"] = original_slot

    def test_render_missing_followup_upload_uses_slot_template_via_upload_service(
        self, monkeypatch,
    ):
        """改 upload frame slot.prompt_template 后 upload_service 文案也跟随。"""
        from app.services.upload_service import _generate_followup_text

        original_frames = slot_schema._frames()
        original_slot = original_frames["job_upload"].slots["headcount"]
        custom = "请告诉我招聘人数，例如 5 人。（字段：{field_display}）"
        original_frames["job_upload"].slots["headcount"] = slot_schema._with_template(
            original_slot, custom,
        )
        try:
            text = _generate_followup_text(["headcount"], frame="job_upload")
            assert "请告诉我招聘人数" in text
        finally:
            original_frames["job_upload"].slots["headcount"] = original_slot

    def test_multi_field_followup_does_not_use_slot_template(self):
        """多字段路径仍用全局模板（slot 级 template 仅适用单字段追问）。"""
        text = slot_schema.render_missing_followup(
            ["city", "job_category"], "job_search", context="search",
        )
        assert "信息还不够完整" in text
        assert "工作城市" in text and "工种" in text
        assert "、" in text
