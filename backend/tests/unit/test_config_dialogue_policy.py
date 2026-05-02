"""阶段四 PR2：DialoguePolicy 嵌套子配置 + 旧字段 / 旧 env 向后兼容单测。

dialogue-intent-extraction-phased-plan §4.1.5。
"""
import os
from contextlib import contextmanager

import pytest

from app.config import DialoguePolicy, Settings


@contextmanager
def _patched_env(**overrides):
    """临时设置 / 清空 env 变量，退出时复原。"""
    original = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, old in original.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


# ---------------------------------------------------------------------------
# 1. DialoguePolicy 子模型：默认值与字段约束
# ---------------------------------------------------------------------------


class TestDialoguePolicyDefaults:
    def test_defaults(self):
        p = DialoguePolicy()
        assert p.v2_mode == "off"
        assert p.shadow_sample_rate == 0.05
        assert p.userid_whitelist == ""
        assert p.hash_buckets == 0
        assert p.primary_rollout_percentage == 0
        assert p.ambiguous_city_query_policy == "clarify"
        assert p.low_confidence_threshold == 0.6
        assert p.search_awaiting_ttl_seconds == 600

    def test_v2_mode_includes_primary_placeholder(self):
        """阶段四 PR2：primary 已加入合法值集（PR3 接通桶后生效）。"""
        for mode in ["off", "shadow", "dual_read", "primary"]:
            p = DialoguePolicy(v2_mode=mode)
            assert p.v2_mode == mode

    def test_v2_mode_invalid_falls_back_to_off(self):
        for bogus in ["bogus", "", "PRIMARY", "  "]:
            p = DialoguePolicy(v2_mode=bogus)
            assert p.v2_mode == "off"

    def test_hash_buckets_clamped(self):
        assert DialoguePolicy(hash_buckets=200).hash_buckets == 100
        assert DialoguePolicy(hash_buckets=-5).hash_buckets == 0
        assert DialoguePolicy(hash_buckets="abc").hash_buckets == 0
        assert DialoguePolicy(hash_buckets=37).hash_buckets == 37

    def test_primary_rollout_percentage_clamped(self):
        # PR3 占位字段，clamp 行为与 hash_buckets 一致
        assert DialoguePolicy(primary_rollout_percentage=200).primary_rollout_percentage == 100
        assert DialoguePolicy(primary_rollout_percentage=-1).primary_rollout_percentage == 0
        assert DialoguePolicy(primary_rollout_percentage=25).primary_rollout_percentage == 25

    def test_acqp_invalid_falls_back_to_clarify(self):
        for bogus in ["bogus", "", "REPLACE", None]:
            p = DialoguePolicy(ambiguous_city_query_policy=bogus)
            assert p.ambiguous_city_query_policy == "clarify"


# ---------------------------------------------------------------------------
# 2. 旧顶层字段名 → dialogue_policy 转发（property + setter）
# ---------------------------------------------------------------------------


class TestLegacyFieldForwarding:
    """所有旧 dialogue_v2_* / ambiguous_city_query_policy / low_confidence_threshold /
    search_awaiting_ttl_seconds 字段读写均转发到 dialogue_policy 同名子字段。
    """

    def test_default_forward_reads(self):
        s = Settings()
        assert s.dialogue_v2_mode == s.dialogue_policy.v2_mode == "off"
        assert s.ambiguous_city_query_policy == s.dialogue_policy.ambiguous_city_query_policy == "clarify"
        assert s.low_confidence_threshold == s.dialogue_policy.low_confidence_threshold == 0.6
        assert s.search_awaiting_ttl_seconds == s.dialogue_policy.search_awaiting_ttl_seconds == 600

    def test_old_kwarg_constructor(self):
        """构造 Settings(dialogue_v2_mode='shadow', ...) 必须把值落到 dialogue_policy。"""
        s = Settings(
            dialogue_v2_mode="shadow",
            dialogue_v2_shadow_sample_rate=0.5,
            dialogue_v2_userid_whitelist="u1,u2",
            dialogue_v2_hash_buckets=25,
            ambiguous_city_query_policy="replace",
            low_confidence_threshold=0.4,
            search_awaiting_ttl_seconds=1200,
        )
        # 旧字段读取 = 新字段读取
        assert s.dialogue_v2_mode == s.dialogue_policy.v2_mode == "shadow"
        assert s.dialogue_v2_shadow_sample_rate == 0.5
        assert s.dialogue_v2_userid_whitelist == "u1,u2"
        assert s.dialogue_v2_hash_buckets == 25
        assert s.ambiguous_city_query_policy == "replace"
        assert s.low_confidence_threshold == 0.4
        assert s.search_awaiting_ttl_seconds == 1200

    def test_setattr_forwards_to_policy(self):
        """测试代码 settings.dialogue_v2_mode = 'x' 模式（test_classify_dialogue_routes 等用法）。"""
        s = Settings()
        s.dialogue_v2_mode = "dual_read"
        s.dialogue_v2_shadow_sample_rate = 1.0
        s.dialogue_v2_userid_whitelist = "u-test-1"
        s.dialogue_v2_hash_buckets = 50
        s.ambiguous_city_query_policy = "replace"
        s.low_confidence_threshold = 0.5
        s.search_awaiting_ttl_seconds = 1800

        # 通过 dialogue_policy 子模型读取应一致
        assert s.dialogue_policy.v2_mode == "dual_read"
        assert s.dialogue_policy.shadow_sample_rate == 1.0
        assert s.dialogue_policy.userid_whitelist == "u-test-1"
        assert s.dialogue_policy.hash_buckets == 50
        assert s.dialogue_policy.ambiguous_city_query_policy == "replace"
        assert s.dialogue_policy.low_confidence_threshold == 0.5
        assert s.dialogue_policy.search_awaiting_ttl_seconds == 1800

    def test_setattr_invalid_value_coerced(self):
        """旧 setter 经 DialoguePolicy 的 mode='before' validator 兜底非法值。"""
        s = Settings()
        s.dialogue_v2_mode = "invalid"
        s.dialogue_v2_hash_buckets = 999
        s.ambiguous_city_query_policy = "bogus"
        assert s.dialogue_v2_mode == "off"
        assert s.dialogue_v2_hash_buckets == 100
        assert s.ambiguous_city_query_policy == "clarify"

    def test_userid_whitelist_set_property(self):
        """生产代码（intent_service._is_dual_read_target）依赖此 property。"""
        s = Settings()
        s.dialogue_v2_userid_whitelist = "  u1 , u2,  u3, ,"
        assert s.dialogue_v2_userid_whitelist_set == {"u1", "u2", "u3"}

        s.dialogue_v2_userid_whitelist = ""
        assert s.dialogue_v2_userid_whitelist_set == set()


# ---------------------------------------------------------------------------
# 3. Env 变量双名兼容 — 旧 > 新 优先级
# ---------------------------------------------------------------------------


class TestLegacyEnvCompat:
    def test_old_env_name_loads_into_policy(self):
        """DIALOGUE_V2_MODE=shadow → settings.dialogue_policy.v2_mode == 'shadow'。"""
        with _patched_env(
            DIALOGUE_V2_MODE="shadow",
            DIALOGUE_V2_HASH_BUCKETS="33",
            AMBIGUOUS_CITY_QUERY_POLICY="replace",
            LOW_CONFIDENCE_THRESHOLD="0.45",
            SEARCH_AWAITING_TTL_SECONDS="900",
        ):
            s = Settings()
            assert s.dialogue_v2_mode == "shadow"
            assert s.dialogue_v2_hash_buckets == 33
            assert s.ambiguous_city_query_policy == "replace"
            assert s.low_confidence_threshold == 0.45
            assert s.search_awaiting_ttl_seconds == 900
            # 通过 nested 同样可读
            assert s.dialogue_policy.v2_mode == "shadow"

    def test_new_nested_env_name_loads(self):
        """DIALOGUE_POLICY__V2_MODE=shadow 通过 env_nested_delimiter 原生支持。"""
        with _patched_env(
            DIALOGUE_POLICY__V2_MODE="shadow",
            DIALOGUE_POLICY__HASH_BUCKETS="42",
            DIALOGUE_V2_MODE=None,  # 显式清空旧 env
            DIALOGUE_V2_HASH_BUCKETS=None,
            AMBIGUOUS_CITY_QUERY_POLICY=None,
            LOW_CONFIDENCE_THRESHOLD=None,
            SEARCH_AWAITING_TTL_SECONDS=None,
        ):
            s = Settings()
            assert s.dialogue_policy.v2_mode == "shadow"
            assert s.dialogue_policy.hash_buckets == 42
            # 旧 property 也读到同样的值
            assert s.dialogue_v2_mode == "shadow"

    def test_old_env_overrides_new_env(self):
        """plan §4.1.5「旧名作为唯一权威源不变，新名只是补充」。"""
        with _patched_env(
            DIALOGUE_V2_MODE="dual_read",
            DIALOGUE_POLICY__V2_MODE="shadow",  # 应被旧 env 覆盖
        ):
            s = Settings()
            assert s.dialogue_v2_mode == "dual_read"
            assert s.dialogue_policy.v2_mode == "dual_read"


# ---------------------------------------------------------------------------
# 4. dev_rollout / golden runner 的 monkeypatch 模式仍工作
# ---------------------------------------------------------------------------


class TestMonkeypatchCompat:
    """复刻 test_dialogue_phase2_dev_rollout / fixtures/dialogue_golden/runner.py
    的 monkeypatch 模式，确保 PR2 改动不破坏现有测试基础设施。
    """

    def test_dev_rollout_save_restore_pattern(self):
        """test_dialogue_phase2_dev_rollout 的典型 save/restore 模式。"""
        s = Settings()
        original_whitelist = s.dialogue_v2_userid_whitelist
        original_buckets = s.dialogue_v2_hash_buckets
        try:
            s.dialogue_v2_userid_whitelist = "u-dev-1,u-dev-2"
            s.dialogue_v2_hash_buckets = 1
            assert s.dialogue_v2_userid_whitelist_set == {"u-dev-1", "u-dev-2"}
            assert s.dialogue_v2_hash_buckets == 1
        finally:
            s.dialogue_v2_userid_whitelist = original_whitelist
            s.dialogue_v2_hash_buckets = original_buckets
        assert s.dialogue_v2_userid_whitelist == ""
        assert s.dialogue_v2_hash_buckets == 0

    def test_dialogue_reducer_acqp_save_restore_pattern(self):
        """test_dialogue_reducer 的 ambiguous_city_query_policy save/restore 模式。"""
        s = Settings()
        original = s.ambiguous_city_query_policy
        try:
            s.ambiguous_city_query_policy = "replace"
            assert s.ambiguous_city_query_policy == "replace"
            s.ambiguous_city_query_policy = "clarify"
            assert s.ambiguous_city_query_policy == "clarify"
        finally:
            s.ambiguous_city_query_policy = original
        assert s.ambiguous_city_query_policy == "clarify"
