"""阶段二反例：worker 角色尝试 job_upload 被拒并 clarify。

worker 不能发布岗位（只允许 job_search / resume_upload）。reducer 在角色权限
校验阶段拦截，输出 clarification.kind=role_no_permission。
"""
CASE = {
    "id": "role_permission_worker_upload",
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
            "user": "我要发布岗位招服务员",
            "mock_llm": {
                "intent": "upload_job",
                "structured_data": {"job_category": "餐饮"},
                "missing_fields": ["city", "salary_floor_monthly", "pay_type", "headcount"],
                "confidence": 0.6,
            },
            "mock_v2": {
                "dialogue_act": "start_upload",
                "frame_hint": "job_upload",
                "slots_delta": {"job_category": ["餐饮"]},
                "merge_hint": {},
                "needs_clarification": False,
                "confidence": 0.7,
                "conflict_action": None,
            },
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "start_upload",
                "resolved_frame": "job_upload",
                "needs_clarification": True,
                "clarification_kind": "role_no_permission",
                "should_run_search": False,
            },
        },
    ],
}
