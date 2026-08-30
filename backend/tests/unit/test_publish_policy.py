from app.config import Settings
from app.listing.job_profile import (
    JOB_ACTIONS, JOB_REQUIRED_FIELDS, job_profile_contract, missing_job_fields,
    normalize_job_fields,
)
from app.services.publish_policy import evaluate_publish_policy, is_rollout_target


def test_job_publish_config_defaults_are_fail_closed():
    configured = Settings(_env_file=None)
    assert configured.job_publish_flow_enabled is False
    assert configured.job_publish_rollout_percentage == 0
    assert configured.job_publish_kill_switch is False
    assert configured.job_publish_action_version == "job_publish_v1"


def test_profile_allowlist_and_required_fields_are_stable():
    values = {"city": " 苏州 ", "job_category": "普工", "phone": "138", "unknown": "drop"}
    assert normalize_job_fields(values) == {"city": "苏州", "job_category": "普工", "phone": "138"}
    assert missing_job_fields(values) == ["headcount", "pay_type", "salary_floor_monthly"]
    contract = job_profile_contract()
    assert set(contract["actions"]) == JOB_ACTIONS
    assert set(contract["required_fields"]) == JOB_REQUIRED_FIELDS


def test_publish_policy_is_deterministic_and_fail_closed():
    kwargs = dict(actor_id="factory-1", role="factory", action_name="publish_job", enabled=True, kill_switch=False, rollout_percentage=100)
    first = evaluate_publish_policy(**kwargs)
    second = evaluate_publish_policy(**kwargs)
    assert first == second and first.allowed
    assert is_rollout_target("factory-1", percentage=100)
    assert evaluate_publish_policy(**{**kwargs, "action_name": "search_job"}).reason == "unsupported_action"
    assert evaluate_publish_policy(**{**kwargs, "role": "worker"}).reason == "role_not_allowed"
    assert evaluate_publish_policy(**{**kwargs, "kill_switch": True}).reason == "kill_switch"
