"""Worker 0 结果默认自动放宽（Phase 5 §5.2 默认行为分支）。

phased-plan §5.2.4 验收 #2：默认行为 auto_relax_and_retry → execute_relaxed_search。

Turn 1：用户首次搜索 → fake_search_jobs 注入 initial_count=0 + 可用放宽步骤
        → reducer 输出 auto_relax_and_retry → applier 调 fake_execute_relaxed_search
        → 二次 reducer 输出 no_action → applier 直出 [mock-relaxed-result]。
"""
CASE = {
    "id": "worker_zero_result_auto_relax",
    "role": "worker",
    "post_search_policy_mode": "on",
    "v2_mode": "dual_read",  # 走 v2 路径让 reducer 真正运行
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
                "structured_data": {
                    "city": ["北京市"], "job_category": ["餐饮"],
                },
                "missing_fields": [],
                "confidence": 0.9,
            },
            # v2 路径：mock_v2 给 DialogueParseResult 让 reducer 跑
            "mock_v2": {
                "dialogue_act": "start_search",
                "frame_hint": "job_search",
                "slots_delta": {"city": ["北京市"], "job_category": ["餐饮"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.9,
            },
            # 让 fake_search_jobs 注入 initial_count=0 + 可用放宽步
            "mock_search_outcome": {
                "initial_count": 0,
                "final_count": 0,
                "available_relax_steps": ["relax_salary_10pct", "broaden_job_category"],
            },
            # 二次检索返回模拟结果
            "mock_relaxed_search": {
                "reply_text": "[mock-relaxed-result]",
                "result_count": 2,
                "applied_step": "relax_salary_10pct",
            },
            "expect": {
                "should_run_search": True,
                # auto_relax_and_retry 触发后 applier 调 fake_execute_relaxed_search，
                # reply 应是放宽结果文案（不带 paginate header）
                "reply_contains": ["[mock-relaxed-result]"],
                "reply_not_contains": ["已经是所有匹配结果了"],
            },
        },
    ],
}
