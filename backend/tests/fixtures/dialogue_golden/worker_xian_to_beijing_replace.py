"""Worker golden case：西安饭店服务员 → 2500 → 北京有吗。

详见 docs/dialogue-intent-extraction-phased-plan.md §1.4.bis。
阶段一断言对应 ``ambiguous_city_query_policy=replace`` 行为；阶段二再新增一份
``policy=clarify`` 的并行 fixture。这里 fixture 顶层显式声明策略，断言由策略驱动。
"""
CASE = {
    "id": "worker_xian_to_beijing_replace",
    "role": "worker",
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
            # 阶段一：worker 搜索护栏要把 LLM 误判 upload_job 纠为 search_job。
            # mock LLM 这里给一份 upload_job 的"漂移"输出，护栏 sanitize 阶段会改写。
            "mock_llm": {
                "intent": "upload_job",
                "structured_data": {
                    "city": "西安市",
                    "job_category": "餐饮",
                },
                "missing_fields": ["pay_type", "headcount"],
                "confidence": 0.7,
            },
            "expect": {
                "intent_not": "upload_job",
                "intent": "search_job",
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                },
                "expected_legacy_missing": [],
                "awaiting_fields": [],
                "should_run_search": True,
                "should_ask_missing": False,
                "needs_clarification": False,
            },
        },
        {
            "user": "2500",
            # 阶段一主路径：已有 search_criteria 时 LLM 应判为 follow_up + 全量快照。
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
            "expect": {
                "intent": "follow_up",
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "expected_legacy_missing": [],
                "should_run_search": True,
                "handler": "_handle_follow_up",
                "needs_clarification": False,
            },
        },
        {
            "user": "北京有吗",
            # follow_up 全量快照：城市替换（policy=replace），不能输出 ["西安市","北京市"]。
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
            "expect": {
                "intent": "follow_up",
                "search_criteria": {
                    "city": ["北京市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                "expected_legacy_missing": [],
                "should_run_search": True,
                "handler": "_handle_follow_up",
                "needs_clarification": False,
            },
        },
    ],
}
