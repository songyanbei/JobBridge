"""Worker 0 结果 + 当前 turn 触碰 salary → ask_clarification 反问 → 用户接受
（Phase 5 §5.2 多轮放宽确认）。

Turn 1：用户搜索时刚断言 salary_floor_monthly=5000；0 命中触发 ask_clarification
       反问 "把薪资放宽 10% 重新搜索吗"，applier 持久化 pending_relaxation。
Turn 2：用户回 "好的"，由 _route_v2_relaxation_response 接管，调
       fake_execute_relaxed_search 拿放宽结果，pending_relaxation 清空。
"""
CASE = {
    "id": "worker_relaxation_offer_accept",
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
                # 反问 "把薪资放宽 10% 重新搜索吗"
                "reply_contains": ["把薪资放宽 10%"],
                # pending_relaxation 已写入；这里 trace 没暴露该字段，
                # 改由 turn 2 验证用户接受后能跑二次检索
            },
        },
        {
            "user": "好的",
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
                "relaxation_response": "accept",
            },
            "mock_relaxed_search": {
                "reply_text": "[mock-relaxed-found-2]",
                "result_count": 2,
                "applied_step": "relax_salary_10pct",
            },
            "expect": {
                "reply_contains": ["[mock-relaxed-found-2]"],
                "reply_not_contains": ["把薪资放宽 10%"],
            },
        },
    ],
}
