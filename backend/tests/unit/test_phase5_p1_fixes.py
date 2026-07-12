"""Phase 5 P1 修复验证（评审第 7 轮）。

三个 P1 阻塞项：
1. search_service 在 post_search_policy_mode=on 时跳过 _run_*_fallback_steps
   让 reducer 接管放宽决策（previously 旧 fallback 已采纳让 reducer 输出 no_action）。
2. _v2_turn_context.parse_result 透传真实 confidence（previously 用 stub 1.0
   让低置信度规则不生效）。
3. phase5_rollout_percentage hash 桶判定（previously on 模式对所有用户生效）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config import settings


# ---------------------------------------------------------------------------
# P1.1：search_service on 模式跳过旧 fallback，由 reducer 接管
# ---------------------------------------------------------------------------


class TestSearchServiceSkipsLegacyFallbackInOnMode:
    """search_jobs 在 post_search_policy_mode=on + 低召回时不再先跑
    _run_job_fallback_steps；available_relax_steps 由 _probe_relax_steps 填好。"""

    @patch("app.services.search_service._query_jobs")
    @patch("app.services.search_service._get_config_int")
    def test_on_mode_skips_run_job_fallback_steps(
        self, mock_config, mock_query, monkeypatch,
    ):
        from app.services.search_service import search_jobs
        from app.schemas.conversation import SessionState
        from app.services.user_service import UserContext

        monkeypatch.setattr(settings, "post_search_policy_mode", "on")
        monkeypatch.setattr(
            settings,
            "dialogue_policy",
            settings.dialogue_policy.model_copy(
                update={"phase5_rollout_percentage": 100},
            ),
        )
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50

        # 全部返回 0 候选：search_jobs 走 "if not candidates: return NO_MATCH" 早返回，
        # 不经过 rerank / format。关键断言：_run_job_fallback_steps 没被调用。
        # _probe_relax_steps 会探查每步——也都返回 0。
        mock_query.return_value = []

        with patch("app.services.search_service._run_job_fallback_steps") as mock_legacy_fb:
            session = SessionState(role="worker")
            user_ctx = UserContext(
                external_userid="u1", role="worker", status="active",
                display_name=None, company=None, contact_person=None, phone=None,
                can_search_jobs=True, can_search_workers=False,
                is_first_touch=False, should_welcome=False,
            )
            result, outcome = search_jobs(
                {"city": ["北京市"], "job_category": ["餐饮"], "salary_floor_monthly": 5000},
                "北京找餐饮", session, user_ctx, MagicMock(),
            )
            # 关键断言：legacy _run_job_fallback_steps 不应被调用
            mock_legacy_fb.assert_not_called()
            # outcome.initial_count=0（_query_jobs 第一次返回空）
            assert outcome.initial_count == 0
            # outcome.available_relax_steps 应已被填充（探查 3 步）
            assert isinstance(outcome.available_relax_steps, list)

    @patch("app.services.search_service._query_jobs")
    @patch("app.services.search_service._get_config_int")
    def test_on_mode_rollout_zero_keeps_legacy_job_fallback(
        self, mock_config, mock_query, monkeypatch,
    ):
        from app.services.search_service import FallbackOutcome, search_jobs
        from app.schemas.conversation import SessionState
        from app.services.user_service import UserContext

        monkeypatch.setattr(settings, "post_search_policy_mode", "on")
        monkeypatch.setattr(
            settings,
            "dialogue_policy",
            settings.dialogue_policy.model_copy(
                update={"phase5_rollout_percentage": 0},
            ),
        )
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50
        mock_query.return_value = []

        with patch("app.services.search_service._run_job_fallback_steps") as mock_legacy_fb:
            mock_legacy_fb.return_value = FallbackOutcome(candidates=[], suggestions=[])
            session = SessionState(role="worker")
            user_ctx = UserContext(
                external_userid="u1", role="worker", status="active",
                display_name=None, company=None, contact_person=None, phone=None,
                can_search_jobs=True, can_search_workers=False,
                is_first_touch=False, should_welcome=False,
            )
            search_jobs(
                {"city": ["北京市"], "job_category": ["餐饮"], "salary_floor_monthly": 5000},
                "北京找餐饮", session, user_ctx, MagicMock(),
            )
            mock_legacy_fb.assert_called_once()

    @patch("app.services.search_service._query_resumes")
    @patch("app.services.search_service._get_config_int")
    def test_on_mode_rollout_zero_keeps_legacy_worker_fallback(
        self, mock_config, mock_query, monkeypatch,
    ):
        from app.services.search_service import FallbackOutcome, search_workers
        from app.schemas.conversation import SessionState
        from app.services.user_service import UserContext

        monkeypatch.setattr(settings, "post_search_policy_mode", "on")
        monkeypatch.setattr(
            settings,
            "dialogue_policy",
            settings.dialogue_policy.model_copy(
                update={"phase5_rollout_percentage": 0},
            ),
        )
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50
        mock_query.return_value = []

        with patch("app.services.search_service._run_resume_fallback_steps") as mock_legacy_fb:
            mock_legacy_fb.return_value = FallbackOutcome(candidates=[], suggestions=[])
            session = SessionState(role="factory")
            user_ctx = UserContext(
                external_userid="u-factory", role="factory", status="active",
                display_name=None, company=None, contact_person=None, phone=None,
                can_search_jobs=False, can_search_workers=True,
                is_first_touch=False, should_welcome=False,
            )
            search_workers(
                {"city": ["北京市"], "job_category": ["餐饮"]},
                "北京找工人", session, user_ctx, MagicMock(),
            )
            mock_legacy_fb.assert_called_once()

    @patch("app.services.search_service._query_jobs")
    @patch("app.services.search_service._get_config_int")
    def test_off_mode_still_uses_legacy_fallback(
        self, mock_config, mock_query, monkeypatch,
    ):
        """off / shadow 模式必须保留旧 _run_job_fallback_steps 行为
        （phased-plan §5.2.4 验收 #2 向后兼容）。"""
        from app.services.search_service import search_jobs
        from app.schemas.conversation import SessionState
        from app.services.user_service import UserContext

        monkeypatch.setattr(settings, "post_search_policy_mode", "off")
        mock_config.side_effect = lambda key, *_: 3 if "top_n" in key else 50
        mock_query.return_value = []  # 全 0 召回让 fallback 触发

        with patch("app.services.search_service._run_job_fallback_steps") as mock_legacy_fb:
            from app.services.search_service import FallbackOutcome
            mock_legacy_fb.return_value = FallbackOutcome(candidates=[], suggestions=[])
            session = SessionState(role="worker")
            user_ctx = UserContext(
                external_userid="u1", role="worker", status="active",
                display_name=None, company=None, contact_person=None, phone=None,
                can_search_jobs=True, can_search_workers=False,
                is_first_touch=False, should_welcome=False,
            )
            search_jobs(
                {"city": ["北京市"], "job_category": ["餐饮"]},
                "北京", session, user_ctx, MagicMock(),
            )
            # off 模式：legacy fallback 必须被调用
            mock_legacy_fb.assert_called_once()


# ---------------------------------------------------------------------------
# P1.2：parse_result.confidence 真实透传
# ---------------------------------------------------------------------------


class TestParseResultConfidenceTranstmittedToReducer:
    """_v2_turn_context 现在存真实 parse_result（含 confidence），让
    _decide_zero_result 的低置信度规则在真实链路可用。"""

    def test_dialogue_route_result_has_parse_result_field(self):
        """DialogueRouteResult.parse_result 字段已存在，默认 None。"""
        from app.services.intent_service import DialogueRouteResult
        from app.llm.base import IntentResult
        r = DialogueRouteResult(
            intent_result=IntentResult(intent="chitchat", confidence=0.5),
            decision=None,
            source="legacy",
        )
        assert r.parse_result is None

    def test_dialogue_route_result_can_carry_parse_result(self):
        from app.services.intent_service import DialogueRouteResult
        from app.llm.base import DialogueParseResult, IntentResult
        parse = DialogueParseResult(
            dialogue_act="start_search", frame_hint="job_search",
            slots_delta={}, merge_hint={}, needs_clarification=False,
            confidence=0.42,
        )
        r = DialogueRouteResult(
            intent_result=IntentResult(intent="search_job", confidence=0.5),
            decision=MagicMock(),
            source="v2_dual_read",
            parse_result=parse,
        )
        assert r.parse_result is parse
        assert r.parse_result.confidence == 0.42

    def test_post_search_dispatch_reads_real_confidence(self, monkeypatch):
        """模拟 _v2_turn_context 含真实 parse_result，验证
        _post_search_dispatch 构造的 PostSearchContext.parse_result 是真实的。"""
        from app.services import message_router
        from app.llm.base import DialogueParseResult
        from app.services.dialogue_reducer import DialogueDecision

        real_parse = DialogueParseResult(
            dialogue_act="start_search", frame_hint="job_search",
            slots_delta={}, merge_hint={}, needs_clarification=False,
            confidence=0.3,  # 低置信度
        )
        real_decision = DialogueDecision(
            dialogue_act="start_search", resolved_frame="job_search",
            route_intent="search_job",
        )
        message_router._set_v2_turn_context(real_parse, real_decision)
        try:
            # 第 8 轮 review fix 2：从 module-level dict 改为 ContextVar
            assert message_router._v2_parse_result.get() is real_parse
            assert message_router._v2_decision.get() is real_decision
        finally:
            message_router._clear_v2_turn_context()
        # 清理后应该为 None
        assert message_router._v2_parse_result.get() is None
        assert message_router._v2_decision.get() is None


# ---------------------------------------------------------------------------
# P1.3：phase5_rollout_percentage hash 桶判定
# ---------------------------------------------------------------------------


class TestPhase5HashBucket:
    def test_percentage_zero_never_matches(self):
        from app.services.intent_service import is_phase5_rollout_target
        assert is_phase5_rollout_target("u-any", 0) is False

    def test_percentage_100_always_matches(self):
        from app.services.intent_service import is_phase5_rollout_target
        assert is_phase5_rollout_target("u-any", 100) is True
        assert is_phase5_rollout_target("u-different", 100) is True

    def test_empty_userid_never_matches(self):
        from app.services.intent_service import is_phase5_rollout_target
        assert is_phase5_rollout_target("", 50) is False
        assert is_phase5_rollout_target("", 100) is False

    def test_deterministic_same_userid(self):
        """同一 userid 多次判定结果一致（hash 稳定）。"""
        from app.services.intent_service import is_phase5_rollout_target
        u = "u-stable"
        r1 = is_phase5_rollout_target(u, 50)
        r2 = is_phase5_rollout_target(u, 50)
        assert r1 == r2

    def test_percentage_threshold_partitions_users(self):
        """50% 灰度应当大约让一半 userid 命中（统计学合理性）。"""
        from app.services.intent_service import is_phase5_rollout_target
        # 100 个 userid 跑 50% 桶
        hits = sum(
            1 for i in range(100)
            if is_phase5_rollout_target(f"user-{i:03d}", 50)
        )
        # 不要求严格 50%，但应在 30-70 范围内（md5 hash 分布合理）
        assert 30 <= hits <= 70, f"hits={hits} far from expected 50 ± 20"

    def test_policy_enabled_combines_mode_and_rollout(self, monkeypatch):
        from app.services.intent_service import is_phase5_policy_enabled

        monkeypatch.setattr(settings, "post_search_policy_mode", "off")
        monkeypatch.setattr(
            settings,
            "dialogue_policy",
            settings.dialogue_policy.model_copy(
                update={"phase5_rollout_percentage": 100},
            ),
        )
        assert is_phase5_policy_enabled("u-any") is False

        monkeypatch.setattr(settings, "post_search_policy_mode", "on")
        monkeypatch.setattr(
            settings,
            "dialogue_policy",
            settings.dialogue_policy.model_copy(
                update={"phase5_rollout_percentage": 100},
            ),
        )
        assert is_phase5_policy_enabled("u-any") is True
        assert is_phase5_policy_enabled("") is False

    def test_post_search_dispatch_off_when_percentage_zero(self, monkeypatch):
        """post_search_policy_mode=on 但 phase5_rollout_percentage=0 → 等价 off。"""
        from app.services import message_router
        from app.schemas.search import SearchOutcome, SearchResult

        monkeypatch.setattr(settings, "post_search_policy_mode", "on")
        monkeypatch.setattr(
            settings,
            "dialogue_policy",
            settings.dialogue_policy.model_copy(
                update={"phase5_rollout_percentage": 0},
            ),
        )

        msg = MagicMock()
        msg.from_user = "u-test"
        msg.content = "test"
        msg.msg_id = "m-1"
        user_ctx = MagicMock()
        user_ctx.role = "worker"
        user_ctx.external_userid = "u-test"
        from app.schemas.conversation import SessionState
        session = SessionState(role="worker")

        # snapshot_exhausted=True 本来 on 模式会输出 paginate_no_more
        outcome = SearchOutcome(
            direction="search_job", criteria_used={"city": ["北京市"]},
            initial_count=0, final_count=0, desired_count=3,
            low_recall_threshold=3, snapshot_exhausted=True,
        )
        replies = message_router._post_search_dispatch(
            msg=msg, user_ctx=user_ctx, session=session, db=MagicMock(),
            search_result=SearchResult(reply_text="原始回复", result_count=0),
            search_outcome=outcome, legacy_intent="show_more",
        )
        # 未命中桶 → 等价 off：返回原始 reply
        assert replies[0].content == "原始回复"

    def test_post_search_dispatch_on_when_percentage_100(self, monkeypatch):
        """phase5_rollout_percentage=100 → 全量命中桶，触发 reducer。"""
        from app.services import message_router
        from app.schemas.search import SearchOutcome, SearchResult
        from app.schemas.conversation import SessionState

        monkeypatch.setattr(settings, "post_search_policy_mode", "on")
        monkeypatch.setattr(
            settings,
            "dialogue_policy",
            settings.dialogue_policy.model_copy(
                update={"phase5_rollout_percentage": 100},
            ),
        )

        msg = MagicMock()
        msg.from_user = "u-test"
        msg.content = "test"
        msg.msg_id = "m-1"
        user_ctx = MagicMock()
        user_ctx.role = "worker"
        user_ctx.external_userid = "u-test"
        session = SessionState(
            role="worker",
            search_criteria={"city": ["北京市"], "job_category": ["餐饮"]},
        )
        outcome = SearchOutcome(
            direction="search_job",
            criteria_used={"city": ["北京市"], "job_category": ["餐饮"]},
            initial_count=0, final_count=0, desired_count=3,
            low_recall_threshold=3, snapshot_exhausted=True,
        )
        replies = message_router._post_search_dispatch(
            msg=msg, user_ctx=user_ctx, session=session, db=MagicMock(),
            search_result=SearchResult(reply_text="原始回复", result_count=0),
            search_outcome=outcome, legacy_intent="show_more",
        )
        # 命中桶 → reducer 输出 paginate_no_more → applier 渲染降级建议
        assert "已经是所有匹配结果了。可以试试以下方向" in replies[0].content

    def test_post_search_dispatch_uses_user_ctx_external_userid(self, monkeypatch):
        """msg.from_user 与 user_ctx.external_userid 不一致时，只以后者判桶。"""
        from app.services import message_router
        from app.schemas.search import SearchOutcome, SearchResult
        from app.schemas.conversation import SessionState
        from app.services.intent_service import is_phase5_rollout_target

        hit_user = next(
            f"hit-{i}" for i in range(1000)
            if is_phase5_rollout_target(f"hit-{i}", 50)
        )
        miss_user = next(
            f"miss-{i}" for i in range(1000)
            if not is_phase5_rollout_target(f"miss-{i}", 50)
        )

        monkeypatch.setattr(settings, "post_search_policy_mode", "on")
        monkeypatch.setattr(
            settings,
            "dialogue_policy",
            settings.dialogue_policy.model_copy(
                update={"phase5_rollout_percentage": 50},
            ),
        )

        msg = MagicMock()
        msg.from_user = hit_user
        msg.content = "test"
        msg.msg_id = "m-1"
        user_ctx = MagicMock()
        user_ctx.role = "worker"
        user_ctx.external_userid = miss_user
        session = SessionState(role="worker")
        outcome = SearchOutcome(
            direction="search_job",
            criteria_used={"city": ["北京市"]},
            initial_count=0, final_count=0, desired_count=3,
            low_recall_threshold=3, snapshot_exhausted=True,
        )

        replies = message_router._post_search_dispatch(
            msg=msg, user_ctx=user_ctx, session=session, db=MagicMock(),
            search_result=SearchResult(reply_text="原始回复", result_count=0),
            search_outcome=outcome, legacy_intent="show_more",
        )

        assert replies[0].content == "原始回复"
