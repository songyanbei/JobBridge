"""Worker 0 结果 + 无可用 relax step → no_action 默认 fallback
（Phase 5 §5.2 默认行为分支兜底）。

phased-plan §5.2.4 验收 #2：默认行为分支应等价当前线上行为。当 search_service
没有可放宽方向（city 字段已极简到无法 broaden 等）时，reducer 输出 no_action，
applier 直出 search_service 原文（含 NO_*_MATCH_REPLY 等）。
"""
CASE = {
    "id": "worker_zero_result_no_relax_steps",
    "role": "worker",
    "post_search_policy_mode": "on",
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
            "user": "北京找餐饮",
            "mock_llm": {
                "intent": "search_job",
                "structured_data": {"city": ["北京市"], "job_category": ["餐饮"]},
                "missing_fields": [],
                "confidence": 0.9,
            },
            "mock_v2": {
                "dialogue_act": "start_search",
                "frame_hint": "job_search",
                "slots_delta": {"city": ["北京市"], "job_category": ["餐饮"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.9,
            },
            "mock_search": {
                "jobs_reply_text": "[mock-no-match-reply]",
            },
            "mock_search_outcome": {
                "initial_count": 0,
                "final_count": 0,
                # 没有可用 relax 方向（极端 criteria 形态）
                "available_relax_steps": [],
            },
            "expect": {
                "should_run_search": True,
                # reducer 输出 no_action → applier 直出 search_result.reply_text
                "reply_contains": ["[mock-no-match-reply]"],
                # 不走二次检索
                "reply_not_contains": ["[mock-relaxed-result]"],
            },
        },
    ],
}
