"""阶段三 reducer schema-driven 单测（dialogue-intent-extraction-phased-plan §3.4.4）。

阶段二 reducer 改造后必须验证：
1. 角色权限拒绝从 slot_schema 派生（worker→job_upload / factory→job_search 拒绝）
2. 非法 enum / 超界 int 值被 _normalize_structured_data 正确 drop
3. soft 字段（provide_meal / provide_housing 等）现在被接受写入 final_search_criteria
4. expected_* synonym 通过 schema.remap_synonyms 在 reducer 入口归并
5. is_list_slot 判定来自 schema（city 列表，salary 标量）
"""
from __future__ import annotations

import pytest

from app.dialogue import slot_schema
from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.services.dialogue_reducer import reduce


def _make_session(role="worker", **kw) -> SessionState:
    base = dict(
        role=role,
        active_flow="idle",
        search_criteria={},
        awaiting_fields=[],
        awaiting_frame=None,
        pending_upload={},
        pending_upload_intent=None,
    )
    base.update(kw)
    return SessionState(**base)


def _make_parse(**kw) -> DialogueParseResult:
    base = dict(
        dialogue_act="chitchat",
        frame_hint="none",
        slots_delta={},
        merge_hint={},
        needs_clarification=False,
        confidence=0.95,
        conflict_action=None,
    )
    base.update(kw)
    return DialogueParseResult(**base)


# ---------------------------------------------------------------------------
# 角色权限：schema-driven check_role_permission
# ---------------------------------------------------------------------------


class TestRolePermissionSchemaDriven:
    def test_worker_start_upload_job_upload_rejected(self):
        """worker 不允许 job_upload；reducer 走 role_no_permission clarification。"""
        s = _make_session(role="worker")
        d = reduce(
            _make_parse(
                dialogue_act="start_upload",
                frame_hint="job_upload",
                slots_delta={"job_category": ["餐饮"]},
            ),
            s, "worker",
        )
        assert d.clarification is not None
        assert d.clarification["kind"] == "role_no_permission"
        assert d.accepted_slots_delta == {}
        assert d.route_intent == "chitchat"

    def test_factory_start_search_job_search_rejected(self):
        """factory 不允许 job_search。"""
        s = _make_session(role="factory")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={"city": ["北京市"]},
            ),
            s, "factory",
        )
        assert d.clarification is not None
        assert d.clarification["kind"] == "role_no_permission"

    def test_factory_resume_upload_rejected(self):
        s = _make_session(role="factory")
        d = reduce(
            _make_parse(
                dialogue_act="start_upload",
                frame_hint="resume_upload",
                slots_delta={"expected_cities": ["北京市"]},
            ),
            s, "factory",
        )
        assert d.clarification is not None
        assert d.clarification["kind"] == "role_no_permission"

    def test_broker_job_search_allowed(self):
        s = _make_session(role="broker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={"city": ["北京市"], "job_category": ["餐饮"]},
            ),
            s, "broker",
        )
        assert d.clarification is None
        assert d.resolved_frame == "job_search"

    def test_broker_candidate_search_allowed(self):
        s = _make_session(role="broker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="candidate_search",
                slots_delta={"city": ["北京市"], "job_category": ["普工"]},
            ),
            s, "broker",
        )
        assert d.clarification is None
        assert d.resolved_frame == "candidate_search"


# ---------------------------------------------------------------------------
# 字段校验：超界 int / 非法 enum / 未知字段
# ---------------------------------------------------------------------------


class TestFieldValidation:
    def test_out_of_range_salary_dropped(self):
        """salary 超出 [500, 200000] 区间被 _normalize_structured_data drop。"""
        s = _make_session(role="worker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={
                    "city": ["北京市"], "job_category": ["餐饮"],
                    "salary_floor_monthly": 999_999,  # 超界
                },
            ),
            s, "worker",
        )
        # salary_floor_monthly 应被 drop
        assert "salary_floor_monthly" not in d.accepted_slots_delta
        # 其它字段正常落
        assert d.accepted_slots_delta.get("city") == ["北京市"]

    def test_negative_headcount_dropped_in_job_upload(self):
        s = _make_session(role="factory")
        d = reduce(
            _make_parse(
                dialogue_act="start_upload",
                frame_hint="job_upload",
                slots_delta={"city": "北京市", "job_category": "餐饮", "headcount": -1},
            ),
            s, "factory",
        )
        assert "headcount" not in d.accepted_slots_delta

    def test_unknown_field_dropped_and_logged(self):
        """schema fields_for(job_search) 不含 totally_unknown_field → drop。"""
        s = _make_session(role="worker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={
                    "city": ["北京市"], "job_category": ["餐饮"],
                    "totally_unknown_field": "x",
                },
            ),
            s, "worker",
        )
        assert "totally_unknown_field" not in d.accepted_slots_delta


# ---------------------------------------------------------------------------
# soft 字段被接受（阶段三关键行为变更）
# ---------------------------------------------------------------------------


class TestSoftFieldsAccepted:
    def test_provide_meal_accepted_in_job_search(self):
        """provide_meal 在 job_search 中 filter_mode=soft；阶段三必须接受写入 final_criteria。"""
        s = _make_session(role="worker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={
                    "city": ["北京市"], "job_category": ["餐饮"],
                    "provide_meal": True,
                },
            ),
            s, "worker",
        )
        assert d.accepted_slots_delta.get("provide_meal") is True
        assert d.final_search_criteria.get("provide_meal") is True

    def test_shift_pattern_accepted_in_job_search(self):
        s = _make_session(role="worker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={
                    "city": ["北京市"], "job_category": ["餐饮"],
                    "shift_pattern": "白班",
                },
            ),
            s, "worker",
        )
        assert d.accepted_slots_delta.get("shift_pattern") == "白班"


# ---------------------------------------------------------------------------
# synonyms remap：expected_cities → city
# ---------------------------------------------------------------------------


class TestSynonymsRemap:
    def test_expected_cities_remapped_to_city_in_job_search(self):
        s = _make_session(role="worker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={
                    "expected_cities": ["北京市"],
                    "job_category": ["餐饮"],
                },
            ),
            s, "worker",
        )
        # 通过 remap_synonyms 后 expected_cities 应转成 city
        assert "expected_cities" not in d.accepted_slots_delta
        assert d.accepted_slots_delta.get("city") == ["北京市"]

    def test_expected_job_categories_remapped_in_candidate_search(self):
        s = _make_session(role="broker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="candidate_search",
                slots_delta={"expected_job_categories": ["普工"]},
            ),
            s, "broker",
        )
        assert "expected_job_categories" not in d.accepted_slots_delta
        assert d.accepted_slots_delta.get("job_category") == ["普工"]


# ---------------------------------------------------------------------------
# is_list_slot 决定 merge policy 路径
# ---------------------------------------------------------------------------


class TestListSlotMergeViaSchema:
    def test_city_with_old_value_and_unknown_hint_uses_clarify_or_replace(self):
        """schema.is_list_slot 让 reducer 进入 list 字段歧义分支。"""
        s = _make_session(
            role="worker", search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
        )
        d = reduce(
            _make_parse(
                dialogue_act="modify_search",
                frame_hint="job_search",
                slots_delta={"city": ["北京市"]},
                merge_hint={"city": "unknown"},
            ),
            s, "worker",
        )
        # 默认 ambiguous_city_query_policy=clarify → 反问
        assert d.clarification is not None
        assert d.clarification["kind"] == "city_replace_or_add"

    def test_salary_with_old_value_and_unknown_hint_replaces(self):
        """salary 标量字段：unknown hint + 有旧值 → schema 默认 replace，不反问。"""
        s = _make_session(
            role="worker",
            search_criteria={
                "city": ["北京市"], "job_category": ["餐饮"],
                "salary_floor_monthly": 5000,
            },
        )
        d = reduce(
            _make_parse(
                dialogue_act="modify_search",
                frame_hint="job_search",
                slots_delta={"salary_floor_monthly": 8000},
                merge_hint={},  # unknown
            ),
            s, "worker",
        )
        assert d.clarification is None
        assert d.final_search_criteria["salary_floor_monthly"] == 8000


# ---------------------------------------------------------------------------
# 低置信度兜底字段集来自 schema
# ---------------------------------------------------------------------------


class TestLowConfidenceKeyFieldsSchemaDriven:
    def test_low_confidence_on_city_triggers_clarify(self):
        s = _make_session(role="worker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={"city": ["北京市"], "job_category": ["餐饮"]},
                confidence=0.3,
            ),
            s, "worker",
        )
        assert d.clarification is not None
        assert d.clarification["kind"] == "low_confidence"

    def test_low_confidence_on_non_key_field_no_clarify(self):
        """provide_meal 不在 key_fields_for_low_confidence → 低置信度也不强制反问。"""
        s = _make_session(role="worker")
        d = reduce(
            _make_parse(
                dialogue_act="start_search",
                frame_hint="job_search",
                slots_delta={"provide_meal": True},
                confidence=0.3,
            ),
            s, "worker",
        )
        # 关键字段未触及，且非 list 歧义路径 → 不应强制反问
        assert (
            d.clarification is None
            or d.clarification["kind"] != "low_confidence"
        )


# ---------------------------------------------------------------------------
# schema 与 reducer 的「权威字段清单」一致性
# ---------------------------------------------------------------------------


class TestSchemaAuthority:
    @pytest.mark.parametrize("frame", [
        "job_search", "candidate_search", "job_upload", "resume_upload",
    ])
    def test_legacy_helper_signature_returns_schema_data(self, frame):
        """phased-plan §3.4.2：reducer 用的字段集合从 schema 派生。"""
        from app.services import intent_service as _is

        assert _is._legacy_valid_fields(frame) == slot_schema.fields_for(frame)
        assert _is._legacy_required(frame) == slot_schema.required_for(frame)
