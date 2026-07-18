"""Worker show_more 翻完降级建议（Phase 5 §5.1）。

phased-plan §5.1.4 验收 #3：worker 翻完时 reducer 输出 paginate_no_more，
applier 根据 session.search_criteria 形态产出具体放宽方向。

case 顶层：
- post_search_policy_mode=on 触发 5.1 paginate_no_more 路径
- mock_show_more.snapshot_exhausted=True 让 fake_show_more 返回翻完 outcome

initial_session.search_criteria 已含 city + job_category + salary_floor_monthly：
- relaxation_directions 应包含 salary_floor / city / job_category 三个方向
- 不含 soft_pref（criteria 中无软偏好字段）
"""
CASE = {
    "id": "worker_show_more_exhausted_paginate",
    "role": "worker",
    "post_search_policy_mode": "on",
    "initial_session": {
        "active_flow": "search_active",
        "broker_direction": None,
        "search_criteria": {
            "city": ["北京市"],
            "job_category": ["餐饮"],
            "salary_floor_monthly": 5000,
        },
        "awaiting_fields": [],
        "awaiting_frame": None,
        "pending_upload": {},
        "pending_upload_intent": None,
        # 模拟之前已搜过 → 有 candidate_snapshot；fake_show_more 不读它，
        # 但 _handle_show_more 用它判断 active_flow 是否保持 search_active。
        "candidate_snapshot": {
            "candidate_ids": ["1"],
            "ranking_version": 1,
            "query_digest": "x" * 12,
            "created_at": "2026-05-10T12:00:00+00:00",
            "expires_at": "2050-01-01T00:00:00+00:00",
            "effective_criteria": {
                "city": ["北京市"],
                "job_category": ["餐饮"],
                "salary_floor_monthly": 5000,
            },
        },
        "shown_items": ["1"],
    },
    "turns": [
        {
            "user": "更多",
            # show_more 由 _match_show_more 关键词短路，不经过 LLM；
            # mock_llm 仍是必填字段，给一份占位（不会被读取）。
            "mock_llm": {
                "intent": "chitchat",
                "structured_data": {},
                "confidence": 0.0,
            },
            # mock fake_show_more 返回 snapshot_exhausted=True
            "mock_show_more": {
                "direction": "search_job",
                "snapshot_exhausted": True,
                "reply_text": "[mock-show-more-exhausted]",
            },
            "expect": {
                "intent": "show_more",
                # _handle_show_more 不在 wrap_handler 包装名单里，handler 字段
                # 为初始空字符串 ""（runner 内 handler_marker 默认值）
                # Phase 5 §5.1：on 模式下 reducer 输出 paginate_no_more
                # → applier 渲染包含 paginate header
                "reply_includes_paginate_header": True,
                "reply_contains": [
                    "下调月薪下限 10%",
                    "换附近城市",
                    "切换工种大类",
                ],
                # 不应再出现旧兜底文案
                "reply_not_contains": [
                    "已经是所有匹配结果了。要不要调整条件重新搜索？",
                ],
            },
        },
    ],
}
