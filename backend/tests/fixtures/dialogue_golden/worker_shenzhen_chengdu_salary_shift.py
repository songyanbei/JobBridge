"""Phase 0 multi-round worker search replay smoke.

Locks the concrete v1 acceptance path from the implementation plan:
深圳 普工 6000 -> add 成都 -> replace salary with 7000 -> exclude night shift.
"""

CASE = {
    "id": "worker_shenzhen_chengdu_salary_shift",
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
            "user": "深圳 普工 6000",
            "mock_llm": {
                "intent": "search_job",
                "structured_data": {
                    "city": ["深圳市"], "job_category": ["普工"],
                    "salary_floor_monthly": 6000,
                },
                "missing_fields": [], "confidence": 0.95,
            },
            "expect": {
                "intent": "search_job", "should_run_search": True,
                "search_criteria": {
                    "city": ["深圳市"], "job_category": ["普工"],
                    "salary_floor_monthly": 6000,
                },
            },
        },
        {
            "user": "成都也可以",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {
                    "city": ["深圳市", "成都市"], "job_category": ["普工"],
                    "salary_floor_monthly": 6000,
                },
                "missing_fields": [], "confidence": 0.92,
            },
            "expect": {
                "intent": "follow_up", "should_run_search": True,
                "search_criteria": {
                    "city": ["深圳市", "成都市"], "job_category": ["普工"],
                    "salary_floor_monthly": 6000,
                },
            },
        },
        {
            "user": "薪资改 7000",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {
                    "city": ["深圳市", "成都市"], "job_category": ["普工"],
                    "salary_floor_monthly": 7000,
                },
                "missing_fields": [], "confidence": 0.92,
            },
            "expect": {
                "intent": "follow_up", "should_run_search": True,
                "search_criteria": {
                    "city": ["深圳市", "成都市"], "job_category": ["普工"],
                    "salary_floor_monthly": 7000,
                },
            },
        },
        {
            "user": "不要夜班",
            "mock_llm": {
                "intent": "follow_up",
                "structured_data": {
                    "city": ["深圳市", "成都市"], "job_category": ["普工"],
                    "salary_floor_monthly": 7000, "shift_pattern": "白班",
                },
                "missing_fields": [], "confidence": 0.9,
            },
            "expect": {
                "intent": "follow_up", "should_run_search": True,
                "search_criteria": {
                    "city": ["深圳市", "成都市"], "job_category": ["普工"],
                    "salary_floor_monthly": 7000, "shift_pattern": "白班",
                },
            },
        },
    ],
}
