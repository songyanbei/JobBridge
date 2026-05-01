"""阶段二 v2 路径下 worker 西安 → 2500 → 北京有吗（policy=clarify，反问）。

与 ``worker_xian_to_beijing_replace_v2.py`` 同前两轮；第三轮触发 clarify：
- reducer 在 has_old_value + merge_hint=unknown + policy=clarify 下输出
  clarification.kind=city_replace_or_add；
- message_router 直接渲染反问文案，**不**调 _handle_follow_up，**不**触发 SQL 检索；
- session.search_criteria 保持 turn 2 末态不变，等用户澄清。
"""
CASE = {
    "id": "worker_xian_to_beijing_clarify",
    "role": "worker",
    "v2_mode": "dual_read",
    "ambiguous_city_query_policy": "clarify",
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
            "mock_llm": {
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
                "merge_hint": {"city": "unknown"},  # 模糊表达 → reducer 接管
                "needs_clarification": False,
                "confidence": 0.85,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "modify_search",
                "resolved_frame": "job_search",
                "needs_clarification": True,
                "clarification_kind": "city_replace_or_add",
                "clarification_options": ["replace", "add"],
                # 反问路径下不写 search_criteria，保持 turn 2 末态
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "should_run_search": False,
            },
        },
    ],
}
