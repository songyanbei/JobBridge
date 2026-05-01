"""阶段二 v2 路径下 broker 机械厂普工 → 北京 → 换成苏州。

与 ``broker_machinery_to_suzhou_replace.py`` 同场景，跑 v2 dual_read：
- 「换成苏州」是明确替换信号，merge_hint=replace；
- 不能叠加成 ["北京市","苏州市"]（Bug 5 防回退点）。
"""
CASE = {
    "id": "broker_machinery_to_suzhou_replace_v2",
    "role": "broker",
    "v2_mode": "dual_read",
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
                "missing_fields": [],
                "confidence": 0.85,
            },
            "mock_v2": {
                "dialogue_act": "start_search",
                "frame_hint": "candidate_search",
                "slots_delta": {"job_category": ["普工"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.9,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "start_search",
                "resolved_frame": "candidate_search",
                "intent": "search_worker",
                "search_criteria": {"job_category": ["普工"]},
                "should_run_search": True,
                "needs_clarification": False,
            },
        },
        {
            "user": "北京",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {"city": ["北京市"], "job_category": ["普工"]},
                "missing_fields": [],
                "confidence": 0.85,
            },
            "mock_v2": {
                "dialogue_act": "modify_search",
                "frame_hint": "candidate_search",
                "slots_delta": {"city": ["北京市"]},
                "merge_hint": {},  # 没旧值时 reducer 自动 replace
                "needs_clarification": False,
                "confidence": 0.9,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "modify_search",
                "resolved_frame": "candidate_search",
                "intent": "follow_up",
                "search_criteria": {
                    "city": ["北京市"],
                    "job_category": ["普工"],
                },
                "final_search_criteria": {
                    "city": ["北京市"],
                    "job_category": ["普工"],
                },
                "should_run_search": True,
                "needs_clarification": False,
            },
        },
        {
            "user": "换成苏州",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {"city": ["苏州市"], "job_category": ["普工"]},
                "missing_fields": [],
                "confidence": 0.9,
            },
            "mock_v2": {
                "dialogue_act": "modify_search",
                "frame_hint": "candidate_search",
                "slots_delta": {"city": ["苏州市"]},
                # 明确替换信号
                "merge_hint": {"city": "replace"},
                "needs_clarification": False,
                "confidence": 0.92,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "modify_search",
                "resolved_frame": "candidate_search",
                "resolved_merge_policy": {"city": "replace"},
                "intent": "follow_up",
                "search_criteria": {
                    "city": ["苏州市"],  # 不能叠加成 ["北京市","苏州市"]
                    "job_category": ["普工"],
                },
                "final_search_criteria": {
                    "city": ["苏州市"],
                    "job_category": ["普工"],
                },
                "should_run_search": True,
                "needs_clarification": False,
            },
        },
    ],
}
