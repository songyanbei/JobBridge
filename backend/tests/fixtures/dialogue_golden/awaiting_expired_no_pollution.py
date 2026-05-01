"""阶段二反例：awaiting 已过期，裸值不再补槽。

initial_session 显式带一个已过期的 awaiting_fields=[salary_floor_monthly]；
用户发"2500"。reducer 检测到 awaiting 过期，**不**走 awaiting tie-break；
LLM 也没抽到字段（mock_v2 空 slots_delta），所以无 accepted slots，最终
不写 search_criteria、不触发 SQL 检索。
"""
import datetime as _dt

# 写一个明确过去时间，绕过 awaiting TTL
_PAST_ISO = (_dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc)).isoformat()


CASE = {
    "id": "awaiting_expired_no_pollution",
    "role": "worker",
    "v2_mode": "dual_read",
    "initial_session": {
        "active_flow": "search_active",
        "broker_direction": None,
        "search_criteria": {
            "city": ["西安市"],
            "job_category": ["餐饮"],
        },
        "awaiting_fields": ["salary_floor_monthly"],
        "awaiting_frame": "job_search",
        "awaiting_expires_at": _PAST_ISO,  # 已过期
        "pending_upload": {},
        "pending_upload_intent": None,
    },
    "turns": [
        {
            "user": "2500",
            "mock_llm": {
                "intent": "chitchat",
                "structured_data": {},
                "missing_fields": [],
                "confidence": 0.3,
            },
            "mock_v2": {
                # LLM 把过期后的裸数值认成 chitchat（没法判定语义）
                "dialogue_act": "chitchat",
                "frame_hint": "none",
                "slots_delta": {},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.3,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "chitchat",
                "resolved_frame": "none",
                # 不污染 search_criteria；保持原值
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                },
                "should_run_search": False,
                "needs_clarification": False,
            },
        },
    ],
}
