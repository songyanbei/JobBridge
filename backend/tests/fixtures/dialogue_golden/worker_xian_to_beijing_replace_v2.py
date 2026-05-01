"""阶段二 v2 路径下 worker 西安 → 2500 → 北京有吗（policy=replace）。

与阶段一 ``worker_xian_to_beijing_replace.py`` 同场景，但跑 v2 dual_read：
- 每 turn 提供 mock_v2 (DialogueParseResult)；
- 断言新增 dialogue_act / resolved_frame / final_search_criteria / source。

policy=replace 路径下「北京有吗」是 modify_search + replace，不反问，
直接走 _handle_follow_up（compat 派生 follow_up）。
"""
CASE = {
    "id": "worker_xian_to_beijing_replace_v2",
    "role": "worker",
    "v2_mode": "dual_read",
    "ambiguous_city_query_policy": "replace",
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
            "user": "西安，想找个饭店的服务员的工作",
            "mock_llm": {  # 仅作为 fallback safety；v2 路径下不应被调用
                "intent": "search_job",
                "structured_data": {"city": "西安市", "job_category": "餐饮"},
                "missing_fields": [],
                "confidence": 0.9,
            },
            "mock_v2": {
                "dialogue_act": "start_search",
                "frame_hint": "job_search",
                "slots_delta": {"city": ["西安市"], "job_category": ["餐饮"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.95,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "start_search",
                "resolved_frame": "job_search",
                "intent": "search_job",
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                },
                "final_search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                },
                "should_run_search": True,
                "needs_clarification": False,
            },
        },
        {
            "user": "2500",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "missing_fields": [],
                "confidence": 0.85,
            },
            "mock_v2": {
                "dialogue_act": "modify_search",
                "frame_hint": "job_search",
                "slots_delta": {"salary_floor_monthly": 2500},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.9,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "modify_search",
                "resolved_frame": "job_search",
                "intent": "follow_up",
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "final_search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "should_run_search": True,
                "needs_clarification": False,
            },
        },
        {
            "user": "北京有吗",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {
                    "city": ["北京市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "missing_fields": [],
                "confidence": 0.82,
            },
            "mock_v2": {
                "dialogue_act": "modify_search",
                "frame_hint": "job_search",
                "slots_delta": {"city": ["北京市"]},
                # 「北京有吗」属于模糊表达：unknown
                "merge_hint": {"city": "unknown"},
                "needs_clarification": False,
                "confidence": 0.85,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "modify_search",
                "resolved_frame": "job_search",
                # policy=replace + has_old_value + hint=unknown → reducer 决策为 replace
                "resolved_merge_policy": {"city": "replace"},
                "intent": "follow_up",
                "search_criteria": {
                    "city": ["北京市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "final_search_criteria": {
                    "city": ["北京市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "should_run_search": True,
                "needs_clarification": False,
            },
        },
    ],
}
