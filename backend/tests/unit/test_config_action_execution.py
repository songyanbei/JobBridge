"""Action/Contact rollout configuration contracts (Workstream C)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_action_configuration_is_defined_once_and_fail_closed():
    source = Path(__file__).parents[2].joinpath("app", "config.py").read_text(encoding="utf-8")
    for field in (
        "action_execution_mode",
        "action_execution_rollout_percentage",
        "action_execution_lease_seconds",
        "action_replay_max_attempts",
        "action_replay_stale_seconds",
        "action_parse_cache_ttl_seconds",
        "action_parse_artifact_retention_seconds",
    ):
        assert len(re.findall(rf"^\s+{field}:\s", source, flags=re.MULTILINE)) == 1

    configured = Settings(_env_file=None)
    assert configured.action_execution_mode == "off"
    assert configured.action_execution_rollout_percentage == 0
    assert configured.action_execution_search_enabled is False
    assert configured.action_show_more_enabled is False
    assert configured.action_relax_enabled is False
    assert configured.contact_service_mode == "off"
    assert configured.action_execution_auto_kill_switch is True


def test_action_configuration_preserves_rollout_and_limit_validators():
    configured = Settings(
        _env_file=None,
        action_execution_mode="shadow",
        action_execution_rollout_percentage=25,
        action_execution_lease_seconds=10,
        action_replay_max_attempts=2,
        action_replay_stale_seconds=20,
        action_parse_cache_ttl_seconds=30,
        action_parse_artifact_retention_seconds=40,
        monitor_action_stale_lease_max_age_seconds=50,
        monitor_action_replay_backlog_max_age_seconds=60,
        monitor_action_replay_backlog_threshold=1,
        monitor_action_missing_reference_threshold=2,
    )
    assert configured.action_execution_mode == "shadow"
    assert configured.action_execution_rollout_percentage == 25
    assert configured.monitor_action_missing_reference_threshold == 2

    with pytest.raises(ValidationError):
        Settings(_env_file=None, action_execution_rollout_percentage=101)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, action_execution_lease_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, monitor_action_replay_backlog_threshold=-1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, action_execution_mode="invalid")
