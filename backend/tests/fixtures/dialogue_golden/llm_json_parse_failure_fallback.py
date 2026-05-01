"""阶段二反例：v2 解析失败 → fallback legacy。

mock_v2 = None 模拟 LLM JSON 解析失败 / provider raise；runner 内部把它当成
LLMParseError，classify_dialogue 退化到 _classify_intent_legacy 内核（不递归）。
此时 source=v2_fallback_legacy，decision=None，路由仍按 legacy intent_result。
"""
CASE = {
    "id": "llm_json_parse_failure_fallback",
    "role": "worker",
    "v2_mode": "dual_read",
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
            "user": "西安找服务员",
            "mock_llm": {
                "intent": "search_job",
                "structured_data": {"city": "西安市", "job_category": "餐饮"},
                "missing_fields": [],
                "confidence": 0.85,
            },
            # 显式模拟 v2 解析失败 → classify_dialogue 退化到 _classify_intent_legacy 内核
            "mock_v2": {"_raise": True},
            "expect": {
                "source": "v2_fallback_legacy",
                "intent": "search_job",
                "search_criteria": {
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                },
                "should_run_search": True,
                "needs_clarification": False,
            },
        },
    ],
}
