"""Worker 0 结果 → ask_clarification → 用户拒绝
（Phase 5 §5.2 cancel_relaxation 路径）。

Turn 1：同 worker_relaxation_offer_accept Turn 1 — ask_clarification 反问。
Turn 2：用户回 "算了"，由 _route_v2_relaxation_response 接 cancel_relaxation 路径，
       渲染 "好的，那我们换其他条件" + 清 pending_relaxation。
"""
CASE = {
    "id": "worker_relaxation_offer_reject",
    "role": "worker",
    "post_search_policy_mode": "on",
    "v2_mode": "dual_read",
    "ambiguous_city_query_policy": "replace",
    "low_confidence_threshold": 0.6,
    "initial_session": {
        "active_flow": "idle",
        "broker_direction": None,
        "search_criteria": {},
        "awaiting_fields": [],
        "awaiting_frame": None,
        "pending_upload": {},
        "pending_upload_intent": None,
    },
    "turns": [
        {
            "user": "北京餐饮 5000 起",
            "mock_llm": {
                "intent": "search_job",
                "structured_data": {
                    "city": ["北京市"], "job_category": ["餐饮"],
                    "salary_floor_monthly": 5000,
                },
                "missing_fields": [],
                "confidence": 0.92,
            },
            "mock_v2": {
                "dialogue_act": "start_search",
                "frame_hint": "job_search",
                "slots_delta": {
                    "city": ["北京市"], "job_category": ["餐饮"],
                    "salary_floor_monthly": 5000,
                },
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.92,
            },
            "mock_search_outcome": {
                "initial_count": 0,
                "final_count": 0,
                "available_relax_steps": ["relax_salary_10pct"],
            },
            "expect": {
                "should_run_search": True,
                "reply_contains": ["把薪资放宽 10%"],
            },
        },
        {
            "user": "算了",
            "mock_llm": {
                "intent": "chitchat", "structured_data": {}, "confidence": 0.0,
            },
            "mock_v2": {
                "dialogue_act": "respond_relaxation_offer",
                "frame_hint": "none",
                "slots_delta": {},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.95,
                "relaxation_response": "reject",
            },
            "expect": {
                "reply_contains": ["好的，那我们换其他条件"],
                "reply_not_contains": ["[mock-relaxed-"],
            },
        },
    ],
}
