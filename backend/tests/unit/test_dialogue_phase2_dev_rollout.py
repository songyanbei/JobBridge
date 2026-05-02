"""阶段二开发期离线验收：用 synthetic 数据替代真实线上流量。

目的：
1. 在未上线、没有真实用户数据的开发阶段，先把 phased-plan §2.5 中与
   shadow / dual-read 相关的验收要求，落成可重复执行的离线测试；
2. 数据集覆盖 worker / factory / broker 三类角色，以及搜索、追问、冲突、
   clarify、权限拒绝、低置信度等典型真实场景；
3. 这组测试只证明“当前代码在一套通用仿真流量上可稳定满足指标”，不替代后续
   上线后的真实 shadow 报表 / dual-read 灰度报表。
"""
from __future__ import annotations

from copy import deepcopy

from app.config import settings
from app.llm.base import DialogueParseResult, IntentResult
from app.schemas.conversation import SessionState
from app.services.dialogue_compat import decision_to_intent_result
from app.services.dialogue_reducer import reduce
from app.services import intent_service
from app.services.intent_service import (
    _is_dual_read_target,
    _sanitize_intent_result,
)
from tests.fixtures.dialogue_golden import (
    active_flow_conflict_upload_to_search,
    broker_machinery_to_suzhou_replace_v2,
    cancel_during_upload_v2,
    resolve_conflict_three_actions,
    worker_xian_to_beijing_replace_v2,
)
from tests.fixtures.dialogue_golden.runner import run_dialogue_case


def _shadow_sample(
    *,
    sample_id: str,
    group: str,
    role: str,
    user: str,
    initial_session: dict,
    mock_llm: dict,
    mock_v2: dict,
    ambiguous_city_query_policy: str | None = None,
    low_confidence_threshold: float | None = None,
) -> dict:
    return {
        "id": sample_id,
        "group": group,
        "role": role,
        "user": user,
        "initial_session": dict(initial_session),
        "mock_llm": dict(mock_llm),
        "mock_v2": dict(mock_v2),
        "ambiguous_city_query_policy": ambiguous_city_query_policy,
        "low_confidence_threshold": low_confidence_threshold,
    }


def _build_shadow_base_samples() -> list[dict]:
    return [
        _shadow_sample(
            sample_id="worker_start_search_aligned",
            group="worker_search",
            role="worker",
            user="西安找餐饮服务员",
            initial_session={
                "active_flow": "idle",
                "search_criteria": {},
                "awaiting_fields": [],
                "awaiting_frame": None,
                "pending_upload": {},
                "pending_upload_intent": None,
            },
            mock_llm={
                "intent": "search_job",
                "structured_data": {"city": ["西安市"], "job_category": ["餐饮"]},
                "missing_fields": [],
                "confidence": 0.9,
            },
            mock_v2={
                "dialogue_act": "start_search",
                "frame_hint": "job_search",
                "slots_delta": {"city": ["西安市"], "job_category": ["餐饮"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.95,
                "conflict_action": None,
            },
        ),
        _shadow_sample(
            sample_id="worker_salary_follow_up_aligned",
            group="worker_search",
            role="worker",
            user="2500",
            initial_session={
                "active_flow": "search_active",
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                },
                "awaiting_fields": [],
                "awaiting_frame": None,
                "pending_upload": {},
                "pending_upload_intent": None,
            },
            mock_llm={
                "intent": "follow_up",
                "structured_data": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "missing_fields": [],
                "confidence": 0.86,
            },
            mock_v2={
                "dialogue_act": "modify_search",
                "frame_hint": "job_search",
                "slots_delta": {"salary_floor_monthly": 2500},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.9,
                "conflict_action": None,
            },
        ),
        _shadow_sample(
            sample_id="broker_start_candidate_search_aligned",
            group="broker_search",
            role="broker",
            user="机械厂普工",
            initial_session={
                "active_flow": "idle",
                "broker_direction": "search_worker",
                "search_criteria": {},
                "awaiting_fields": [],
                "awaiting_frame": None,
                "pending_upload": {},
                "pending_upload_intent": None,
            },
            mock_llm={
                "intent": "search_worker",
                "structured_data": {"job_category": ["普工"]},
                "missing_fields": [],
                "confidence": 0.84,
            },
            mock_v2={
                "dialogue_act": "start_search",
                "frame_hint": "candidate_search",
                "slots_delta": {"job_category": ["普工"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.9,
                "conflict_action": None,
            },
        ),
        _shadow_sample(
            sample_id="broker_city_replace_aligned",
            group="broker_search",
            role="broker",
            user="换成苏州",
            initial_session={
                "active_flow": "search_active",
                "broker_direction": "search_worker",
                "search_criteria": {
                    "city": ["北京市"],
                    "job_category": ["普工"],
                },
                "awaiting_fields": [],
                "awaiting_frame": None,
                "pending_upload": {},
                "pending_upload_intent": None,
            },
            mock_llm={
                "intent": "follow_up",
                "structured_data": {"city": ["苏州市"], "job_category": ["普工"]},
                "missing_fields": [],
                "confidence": 0.9,
            },
            mock_v2={
                "dialogue_act": "modify_search",
                "frame_hint": "candidate_search",
                "slots_delta": {"city": ["苏州市"]},
                "merge_hint": {"city": "replace"},
                "needs_clarification": False,
                "confidence": 0.92,
                "conflict_action": None,
            },
            ambiguous_city_query_policy="replace",
        ),
        _shadow_sample(
            sample_id="worker_ambiguous_city_clarify",
            group="clarify",
            role="worker",
            user="北京有吗",
            initial_session={
                "active_flow": "search_active",
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "awaiting_fields": [],
                "awaiting_frame": None,
                "pending_upload": {},
                "pending_upload_intent": None,
            },
            mock_llm={
                "intent": "follow_up",
                "structured_data": {
                    "city": ["北京市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "missing_fields": [],
                "confidence": 0.82,
            },
            mock_v2={
                "dialogue_act": "modify_search",
                "frame_hint": "job_search",
                "slots_delta": {"city": ["北京市"]},
                "merge_hint": {"city": "unknown"},
                "needs_clarification": False,
                "confidence": 0.85,
                "conflict_action": None,
            },
            ambiguous_city_query_policy="clarify",
        ),
        _shadow_sample(
            sample_id="upload_collecting_conflict",
            group="upload_conflict",
            role="broker",
            user="先帮我找个普工",
            initial_session={
                "active_flow": "upload_collecting",
                "search_criteria": {},
                "awaiting_fields": [],
                "awaiting_frame": None,
                "pending_upload": {
                    "city": "北京市",
                    "job_category": "餐饮",
                    "salary_floor_monthly": 6000,
                    "pay_type": "月薪",
                },
                "pending_upload_intent": "upload_job",
                "awaiting_field": "headcount",
            },
            mock_llm={
                "intent": "search_worker",
                "structured_data": {"job_category": ["普工"]},
                "missing_fields": [],
                "confidence": 0.85,
            },
            mock_v2={
                "dialogue_act": "start_search",
                "frame_hint": "candidate_search",
                "slots_delta": {"job_category": ["普工"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.9,
                "conflict_action": None,
            },
        ),
        _shadow_sample(
            sample_id="worker_role_no_permission",
            group="role_boundary",
            role="worker",
            user="我要发布岗位招服务员",
            initial_session={
                "active_flow": "idle",
                "search_criteria": {},
                "awaiting_fields": [],
                "awaiting_frame": None,
                "pending_upload": {},
                "pending_upload_intent": None,
            },
            mock_llm={
                "intent": "upload_job",
                "structured_data": {"city": "北京市", "job_category": "餐饮"},
                "missing_fields": ["pay_type", "headcount"],
                "confidence": 0.86,
            },
            mock_v2={
                "dialogue_act": "start_upload",
                "frame_hint": "job_upload",
                "slots_delta": {"city": "北京市", "job_category": "餐饮"},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.93,
                "conflict_action": None,
            },
        ),
        _shadow_sample(
            sample_id="worker_low_confidence",
            group="low_confidence",
            role="worker",
            user="北京的活有吗",
            initial_session={
                "active_flow": "idle",
                "search_criteria": {},
                "awaiting_fields": [],
                "awaiting_frame": None,
                "pending_upload": {},
                "pending_upload_intent": None,
            },
            mock_llm={
                "intent": "search_job",
                "structured_data": {"city": ["北京市"]},
                "missing_fields": [],
                "confidence": 0.7,
            },
            mock_v2={
                "dialogue_act": "start_search",
                "frame_hint": "job_search",
                "slots_delta": {"city": ["北京市"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.4,
                "conflict_action": None,
            },
            low_confidence_threshold=0.6,
        ),
    ]


def _build_shadow_synthetic_week() -> list[dict]:
    base = _build_shadow_base_samples()
    week: list[dict] = []
    for idx in range(7):
        rotated = base[idx % len(base):] + base[:idx % len(base)]
        week.append({
            "day": f"2026-04-{idx + 1:02d}",
            "traffic_total": 120,
            "samples": rotated,
        })
    return week


def _empty_job_search_case() -> dict:
    return {
        "id": "worker_job_search_empty",
        "role": "worker",
        "v2_mode": "dual_read",
        "initial_session": {
            "active_flow": "idle",
            "search_criteria": {},
            "awaiting_fields": [],
            "awaiting_frame": None,
            "pending_upload": {},
            "pending_upload_intent": None,
        },
        "turns": [
            {
                "user": "深圳电子厂有吗",
                "mock_llm": {
                    "intent": "search_job",
                    "structured_data": {"city": ["深圳市"], "job_category": ["电子厂"]},
                    "missing_fields": [],
                    "confidence": 0.9,
                },
                "mock_v2": {
                    "dialogue_act": "start_search",
                    "frame_hint": "job_search",
                    "slots_delta": {"city": ["深圳市"], "job_category": ["电子厂"]},
                    "merge_hint": {},
                    "needs_clarification": False,
                    "confidence": 0.94,
                    "conflict_action": None,
                },
                "mock_search": {"jobs_reply_text": "[mock-empty-jobs-result]"},
                "expect": {
                    "should_run_search": True,
                    "search_criteria": {"city": ["深圳市"], "job_category": ["电子厂"]},
                },
            },
        ],
    }


def _empty_worker_search_case() -> dict:
    return {
        "id": "factory_worker_search_empty",
        "role": "factory",
        "v2_mode": "dual_read",
        "initial_session": {
            "active_flow": "idle",
            "search_criteria": {},
            "awaiting_fields": [],
            "awaiting_frame": None,
            "pending_upload": {},
            "pending_upload_intent": None,
        },
        "turns": [
            {
                "user": "苏州普工有吗",
                "mock_llm": {
                    "intent": "search_worker",
                    "structured_data": {"city": ["苏州市"], "job_category": ["普工"]},
                    "missing_fields": [],
                    "confidence": 0.88,
                },
                "mock_v2": {
                    "dialogue_act": "start_search",
                    "frame_hint": "candidate_search",
                    "slots_delta": {"city": ["苏州市"], "job_category": ["普工"]},
                    "merge_hint": {},
                    "needs_clarification": False,
                    "confidence": 0.92,
                    "conflict_action": None,
                },
                "mock_search": {"workers_reply_text": "[mock-empty-workers-result]"},
                "expect": {
                    "should_run_search": True,
                    "search_criteria": {"city": ["苏州市"], "job_category": ["普工"]},
                },
            },
        ],
    }


def _clone_case(case: dict, *, case_id: str, mode: str) -> dict:
    cloned = deepcopy(case)
    cloned["id"] = case_id
    cloned["v2_mode"] = mode
    return cloned


def _build_dual_read_rollout_days() -> list[dict]:
    base_cases = [
        worker_xian_to_beijing_replace_v2.CASE,
        broker_machinery_to_suzhou_replace_v2.CASE,
        _empty_job_search_case(),
        _empty_worker_search_case(),
        active_flow_conflict_upload_to_search.CASE,
        resolve_conflict_three_actions.CASE_RESUME,
        resolve_conflict_three_actions.CASE_PROCEED,
        cancel_during_upload_v2.CASE,
    ]
    return [
        {"day": "2026-04-08", "cases": [deepcopy(base_cases[i]) for i in (0, 1, 2, 4, 7)]},
        {"day": "2026-04-09", "cases": [deepcopy(base_cases[i]) for i in (3, 4, 5, 6, 0)]},
        {"day": "2026-04-10", "cases": [deepcopy(base_cases[i]) for i in (1, 2, 3, 6, 7)]},
    ]


def _normalize_payload(payload: dict) -> dict:
    normalized: dict = {}
    for key, value in (payload or {}).items():
        if isinstance(value, list):
            normalized[key] = sorted(value)
        else:
            normalized[key] = value
    return normalized


def _payload_is_subset(partial: dict, full: dict) -> bool:
    partial_norm = _normalize_payload(partial)
    full_norm = _normalize_payload(full)
    for key, value in partial_norm.items():
        if key not in full_norm or full_norm[key] != value:
            return False
    return True


def _shadow_compare(sample: dict) -> dict:
    session = SessionState(role=sample["role"], **dict(sample["initial_session"]))
    original_policy = getattr(settings, "ambiguous_city_query_policy", "clarify")
    original_threshold = getattr(settings, "low_confidence_threshold", 0.6)
    try:
        if sample.get("ambiguous_city_query_policy") is not None:
            settings.ambiguous_city_query_policy = sample["ambiguous_city_query_policy"]
        if sample.get("low_confidence_threshold") is not None:
            settings.low_confidence_threshold = float(sample["low_confidence_threshold"])

        legacy = _sanitize_intent_result(
            IntentResult(**sample["mock_llm"]),
            sample["role"],
            raw_text=sample["user"].strip(),
        )
        parse = DialogueParseResult(**sample["mock_v2"])
        decision = reduce(parse, session, sample["role"], raw_text=sample["user"].strip())
        v2_ir = decision_to_intent_result(decision, session)
    finally:
        settings.ambiguous_city_query_policy = original_policy
        settings.low_confidence_threshold = original_threshold

    effective_v2_route = "clarification" if decision.clarification else v2_ir.intent
    legacy_payload = dict(legacy.structured_data or {})
    v2_delta = dict(decision.accepted_slots_delta or {})

    return {
        "id": sample["id"],
        "group": sample["group"],
        "intent_diff": effective_v2_route != legacy.intent,
        "frame_conflict": decision.resolved_frame != parse.frame_hint,
        "clarification": bool(decision.clarification),
        "field_match": _payload_is_subset(v2_delta, legacy_payload),
        "state_transition": decision.state_transition,
        "legacy_intent": legacy.intent,
        "v2_route": effective_v2_route,
    }


def _summarize_shadow_day(day: dict) -> dict:
    rows = [_shadow_compare(sample) for sample in day["samples"]]
    sampled = len(rows)
    anomalies = [
        row["id"] for row in rows
        if (
            row["intent_diff"]
            or row["frame_conflict"]
            or row["clarification"]
            or row["state_transition"] == "enter_upload_conflict"
        )
    ]
    return {
        "day": day["day"],
        "traffic_total": day["traffic_total"],
        "sampled_requests": sampled,
        "sample_rate": sampled / float(day["traffic_total"] or 1),
        "intent_misroute_rate": sum(1 for row in rows if row["intent_diff"]) / float(sampled or 1),
        "frame_conflict_rate": sum(1 for row in rows if row["frame_conflict"]) / float(sampled or 1),
        "clarification_rate": sum(1 for row in rows if row["clarification"]) / float(sampled or 1),
        "field_extraction_match_rate": sum(1 for row in rows if row["field_match"]) / float(sampled or 1),
        "anomaly_case_ids": anomalies,
    }


def _first_search_turn(result: dict) -> int | None:
    for idx, turn in enumerate(result["turns"], start=1):
        if turn["ran_search"]:
            return idx
    return None


def _is_empty_search_reply(reply_text: str) -> bool:
    return reply_text.startswith("[mock-empty-")


def _aggregate_rollout_metrics(results: list[dict]) -> dict:
    total_searches = 0
    empty_searches = 0
    conflicts = 0
    turns_to_search_sum = 0
    searched_dialogues = 0

    for result in results:
        search_results = list(result["spy"].jobs_results) + list(result["spy"].workers_results)
        total_searches += len(search_results)
        empty_searches += sum(
            1 for item in search_results if _is_empty_search_reply(item["reply_text"])
        )
        if any(
            turn["state_transition"] == "enter_upload_conflict"
            or turn["session_active_flow"] == "upload_conflict"
            for turn in result["turns"]
        ):
            conflicts += 1
        first_search_turn = _first_search_turn(result)
        if first_search_turn is not None:
            searched_dialogues += 1
            turns_to_search_sum += first_search_turn

    total_dialogues = len(results)
    return {
        "total_dialogues": total_dialogues,
        "total_searches": total_searches,
        "empty_search_result_rate": empty_searches / float(total_searches or 1),
        "upload_conflict_rate": conflicts / float(total_dialogues or 1),
        "avg_turns_to_search": turns_to_search_sum / float(searched_dialogues or 1),
    }


def test_phase2_shadow_synthetic_weekly_report():
    """开发期替代 shadow 一周报表：至少 5% 采样，且能产出日级 diff 摘要。"""
    original_city_cache = intent_service._CITY_LOOKUP_CACHE
    try:
        # 开发期 synthetic 测试不依赖真实 MySQL 字典；直接用内置常见城市映射，
        # 避免每条样本都因 DB 不可达而重试、拖慢 shadow 周报测试。
        intent_service._CITY_LOOKUP_CACHE = dict(intent_service._COMMON_CITY_ALIASES)
        week = _build_shadow_synthetic_week()
        summaries = [_summarize_shadow_day(day) for day in week]
    finally:
        intent_service._CITY_LOOKUP_CACHE = original_city_cache

    assert len(summaries) == 7
    required_keys = {
        "day",
        "traffic_total",
        "sampled_requests",
        "sample_rate",
        "intent_misroute_rate",
        "frame_conflict_rate",
        "clarification_rate",
        "field_extraction_match_rate",
        "anomaly_case_ids",
    }
    for row in summaries:
        assert set(row.keys()) == required_keys
        assert row["sample_rate"] >= 0.05
        assert row["sampled_requests"] == len(_build_shadow_base_samples())

    anomaly_ids = {
        case_id
        for row in summaries
        for case_id in row["anomaly_case_ids"]
    }
    assert {
        "worker_ambiguous_city_clarify",
        "upload_collecting_conflict",
        "worker_role_no_permission",
        "worker_low_confidence",
    }.issubset(anomaly_ids)


def test_phase2_dual_read_synthetic_rollout():
    """开发期替代 dual-read 3 天灰度：5 个白名单 + 1% hash 桶 + 指标不回退。"""
    whitelist_users = [f"internal-u{i}" for i in range(1, 6)]
    original_whitelist = settings.dialogue_v2_userid_whitelist
    original_buckets = settings.dialogue_v2_hash_buckets
    try:
        settings.dialogue_v2_userid_whitelist = ",".join(whitelist_users)
        settings.dialogue_v2_hash_buckets = 1

        assert all(_is_dual_read_target(userid) for userid in whitelist_users)
        bucket_users = [f"gray-user-{idx:04d}" for idx in range(1000)]
        bucket_hit_rate = sum(
            1 for userid in bucket_users if _is_dual_read_target(userid)
        ) / float(len(bucket_users))
        assert 0.005 <= bucket_hit_rate <= 0.015
    finally:
        settings.dialogue_v2_userid_whitelist = original_whitelist
        settings.dialogue_v2_hash_buckets = original_buckets

    legacy_results: list[dict] = []
    dual_results: list[dict] = []
    for day in _build_dual_read_rollout_days():
        for idx, case in enumerate(day["cases"]):
            legacy_case = _clone_case(
                case, case_id=f"{day['day']}-{idx}-{case['id']}-legacy", mode="off",
            )
            dual_case = _clone_case(
                case, case_id=f"{day['day']}-{idx}-{case['id']}-dual", mode="dual_read",
            )
            legacy_results.append(run_dialogue_case(legacy_case))
            dual_results.append(run_dialogue_case(dual_case))

    legacy_metrics = _aggregate_rollout_metrics(legacy_results)
    dual_metrics = _aggregate_rollout_metrics(dual_results)

    assert dual_metrics["total_dialogues"] == legacy_metrics["total_dialogues"] == 15
    assert dual_metrics["empty_search_result_rate"] <= (
        legacy_metrics["empty_search_result_rate"] + 0.01
    )
    assert dual_metrics["upload_conflict_rate"] <= (
        legacy_metrics["upload_conflict_rate"] + 0.01
    )
    assert dual_metrics["avg_turns_to_search"] <= (
        legacy_metrics["avg_turns_to_search"] + 0.2
    )
