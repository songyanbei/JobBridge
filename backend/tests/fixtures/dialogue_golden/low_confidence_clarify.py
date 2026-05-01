"""阶段二反例：关键字段低置信度 → reducer 强制 needs_clarification=true。

用户文本表达模糊；LLM 低置信度抽出关键字段（city / job_category / salary_*）。
reducer 检测到 confidence < low_confidence_threshold 且本轮触及关键字段，
override LLM 的 needs_clarification=False，输出 clarification.kind=low_confidence。
"""
CASE = {
    "id": "low_confidence_clarify",
    "role": "worker",
    "v2_mode": "dual_read",
    # 阈值默认 0.6；此 case 显式声明便于阅读
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
            "user": "我想找点活儿",
            "mock_llm": {
                "intent": "search_job",
                "structured_data": {"job_category": ["其他"]},
                "missing_fields": ["city"],
                "confidence": 0.4,
            },
            "mock_v2": {
                "dialogue_act": "start_search",
                "frame_hint": "job_search",
                "slots_delta": {"job_category": ["其他"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.4,  # 低于阈值且触及 job_category
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "start_search",
                "resolved_frame": "job_search",
                "needs_clarification": True,
                "clarification_kind": "low_confidence",
                "should_run_search": False,
            },
        },
    ],
}
