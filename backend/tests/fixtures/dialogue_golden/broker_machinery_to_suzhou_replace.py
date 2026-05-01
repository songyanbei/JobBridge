"""Broker golden case：机械厂普工 → 北京 → 换成苏州。

详见 docs/dialogue-intent-extraction-phased-plan.md §1.5（broker case 退出标准）。
"换成 X" 必须替换城市，绝不可叠加成 ["北京市","苏州市"]（fdeb18d Bug 5 修复点）。
"""
CASE = {
    "id": "broker_machinery_to_suzhou_replace",
    "role": "broker",
    "ambiguous_city_query_policy": "replace",
    "initial_session": {
        "active_flow": "idle",
        "broker_direction": "search_worker",
        "search_criteria": {},
        "awaiting_fields": [],
        "awaiting_frame": None,
        "pending_upload": {},
        "pending_upload_intent": None,
    },
    "turns": [
        {
            "user": "机械厂普工",
            "mock_llm": {
                "intent": "search_worker",
                "structured_data": {"job_category": ["普工"]},
                "missing_fields": ["city"],
                "confidence": 0.8,
            },
            "expect": {
                "intent": "search_worker",
                "search_criteria": {"job_category": ["普工"]},
                # candidate_search 用 required_any={city, job_category}；job_category 已填，
                # legacy schema 计算的 missing 应为空（即便 LLM 自己上报 city missing）。
                "expected_legacy_missing": [],
                "should_run_search": True,
                "should_ask_missing": False,
                "needs_clarification": False,
            },
        },
        {
            "user": "北京",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {
                    "city": ["北京市"],
                    "job_category": ["普工"],
                },
                "missing_fields": [],
                "confidence": 0.85,
            },
            "expect": {
                "intent": "follow_up",
                "search_criteria": {
                    "city": ["北京市"],
                    "job_category": ["普工"],
                },
                "expected_legacy_missing": [],
                "should_run_search": True,
                "handler": "_handle_follow_up",
                "needs_clarification": False,
            },
        },
        {
            "user": "换成苏州",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {
                    "city": ["苏州市"],
                    "job_category": ["普工"],
                },
                "missing_fields": [],
                "confidence": 0.9,
            },
            "expect": {
                "intent": "follow_up",
                "search_criteria": {
                    # 关键断言：必须替换为苏州，不能是 ["北京市", "苏州市"]
                    "city": ["苏州市"],
                    "job_category": ["普工"],
                },
                "expected_legacy_missing": [],
                "should_run_search": True,
                "handler": "_handle_follow_up",
                "needs_clarification": False,
            },
        },
    ],
}
