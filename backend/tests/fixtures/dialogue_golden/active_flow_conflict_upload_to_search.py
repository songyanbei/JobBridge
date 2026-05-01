"""阶段二反例：upload_collecting 中说"先帮我找个普工" → enter_upload_conflict。

active_flow=upload_collecting 时，frame_hint=candidate_search 应被 reducer 翻译成
state_transition=enter_upload_conflict + pending_interruption（current-state §3.1）。
applier 写入 session.active_flow=upload_conflict + pending_interruption；
后续由 message_router 现成的冲突 handler 处理（不再复制冲突逻辑）。
"""
CASE = {
    "id": "active_flow_conflict_upload_to_search",
    "role": "broker",
    "v2_mode": "dual_read",
    "initial_session": {
        "active_flow": "upload_collecting",
        "broker_direction": None,
        "search_criteria": {},
        "awaiting_fields": [],
        "awaiting_frame": None,
        # 上传草稿已经收集到部分字段，仍在缺 headcount
        "pending_upload": {
            "city": "北京市",
            "job_category": "餐饮",
            "salary_floor_monthly": 6000,
            "pay_type": "月薪",
        },
        "pending_upload_intent": "upload_job",
        "awaiting_field": "headcount",
    },
    "turns": [
        {
            "user": "先帮我找个普工",
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
                "state_transition": "enter_upload_conflict",
                # 不应触发实际 SQL 检索（要先解决冲突）
                "should_run_search": False,
                "needs_clarification": False,
            },
        },
    ],
}
