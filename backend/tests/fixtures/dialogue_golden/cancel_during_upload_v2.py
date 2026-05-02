"""阶段二反例：upload_collecting 中发 /取消，即使 LLM 把它误判为业务意图，
仍按 cancel 处理（codex review P2 防回归）。

phased-plan §2.5.5 验收必测项：
> /取消 即使 LLM 给了 start_search，仍按 cancel 处理

关键路径：classify_dialogue 在 mode != off 时入口先跑 _match_command；
"/取消" 命中 cancel_pending 直接短路返回，根本不调 extract_dialogue。
所以即使 mock_v2 给一个错误的 start_search 派遣，本 turn 也不会用到。
"""
CASE = {
    "id": "cancel_during_upload_v2",
    "role": "factory",
    "v2_mode": "dual_read",
    "initial_session": {
        "active_flow": "upload_collecting",
        "broker_direction": None,
        "search_criteria": {},
        "awaiting_fields": [],
        "awaiting_frame": None,
        "pending_upload": {
            "job_category": "餐饮",
            "city": "北京市",
            "salary_floor_monthly": 4000,
            "pay_type": "月薪",
        },
        "pending_upload_intent": "upload_job",
        "awaiting_field": "headcount",
        "pending_started_at": "2099-01-01T00:00:00+00:00",
        "pending_updated_at": "2099-01-01T00:00:00+00:00",
        "pending_expires_at": "2099-01-01T00:10:00+00:00",
    },
    "turns": [
        {
            "user": "/取消",
            # legacy 的 IntentResult 备份（mode=off 时用）—— 命令短路其实不会用到
            "mock_llm": {
                "intent": "command",
                "structured_data": {"command": "cancel_pending"},
                "confidence": 1.0,
            },
            # 故意给一个错误的 LLM v2 输出，验证 /取消 字面命令能抢占 LLM 误判
            "mock_v2": {
                "dialogue_act": "start_search",
                "frame_hint": "candidate_search",
                "slots_delta": {"job_category": ["餐饮"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.8,
                "conflict_action": None,
            },
            "expect": {
                # /取消 命中 _match_command 短路 → source=legacy（不调 v2）
                # ReplyMessage.intent 在 cancel_pending 路径上不会被设置，所以不断言；
                # 主断言：source=legacy（确认没走 v2 路径）+ 不触发 SQL 检索。
                "source": "legacy",
                "should_run_search": False,
            },
        },
    ],
}
