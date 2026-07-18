"""Broker 找工人 show_more 翻完降级建议（Phase 5 §5.1）。

phased-plan §5.1.4 验收 #3：broker / factory 在 candidate_search 方向翻完时，
relaxation_directions 应使用 salary_ceiling 而非 salary_floor。

case 顶层：
- post_search_policy_mode=on 触发 5.1 paginate_no_more 路径
- broker_direction=search_worker 让 _handle_show_more 走 candidate_search frame
- mock_show_more.direction=search_worker

initial_session.search_criteria 含 city + job_category + salary_ceiling_monthly：
- relaxation_directions 应包含 salary_ceiling / city / job_category 三个方向
- 文案使用"放宽期望薪资上限 / 换其他城市 / 切换工种大类"
"""
CASE = {
    "id": "broker_show_more_exhausted_paginate",
    "role": "broker",
    "post_search_policy_mode": "on",
    "initial_session": {
        "active_flow": "search_active",
        "broker_direction": "search_worker",
        "search_criteria": {
            "city": ["苏州市"],
            "job_category": ["机械"],
            "salary_ceiling_monthly": 6000,
        },
        "awaiting_fields": [],
        "awaiting_frame": None,
        "pending_upload": {},
        "pending_upload_intent": None,
        "candidate_snapshot": {
            "candidate_ids": ["1"],
            "ranking_version": 1,
            "query_digest": "y" * 12,
            "created_at": "2026-05-10T12:00:00+00:00",
            "expires_at": "2050-01-01T00:00:00+00:00",
            "effective_criteria": {
                "city": ["苏州市"],
                "job_category": ["机械"],
                "salary_ceiling_monthly": 6000,
            },
        },
        "shown_items": ["1"],
    },
    "turns": [
        {
            "user": "还有吗",
            "mock_llm": {
                "intent": "chitchat",
                "structured_data": {},
                "confidence": 0.0,
            },
            "mock_show_more": {
                "direction": "search_worker",
                "snapshot_exhausted": True,
                "reply_text": "[mock-show-more-exhausted]",
            },
            "expect": {
                "intent": "show_more",
                # candidate_search frame 应使用 salary_ceiling 文案
                "reply_includes_paginate_header": True,
                "reply_contains": [
                    "放宽期望薪资上限",
                    "换其他城市",
                    "切换工种大类",
                ],
                # 不应出现 job_search 视角的"下调月薪下限 10%"
                "reply_not_contains": [
                    "下调月薪下限 10%",
                    "已经是所有匹配结果了。要不要调整条件重新搜索？",
                ],
            },
        },
    ],
}
