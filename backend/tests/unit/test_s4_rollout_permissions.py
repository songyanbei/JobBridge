from types import SimpleNamespace

from app.services.permission_service import can_manage_job, can_publish_job
from scripts.s4_rollout import actor_bucket, evaluate_gate, rollout_assignment, rollback_plan


def test_publisher_matrix_is_fail_closed():
    factory = SimpleNamespace(role="factory", status="active", external_userid="f1")
    broker = SimpleNamespace(role="broker", status="active", external_userid="b1", can_search_workers=True)
    worker = SimpleNamespace(role="worker", status="active", external_userid="w1")
    assert can_publish_job(factory, owner_userid="f1")
    assert can_publish_job(broker, owner_userid="b1")
    assert not can_publish_job(worker, owner_userid="w1")
    assert not can_publish_job(factory, owner_userid="other")
    assert can_manage_job(factory, SimpleNamespace(owner_userid="f1"))


def test_rollout_bucket_is_stable_and_clamped():
    assert actor_bucket("f1") == actor_bucket("f1")
    assert rollout_assignment("f1", 100)
    assert not rollout_assignment("f1", 0)
    assert evaluate_gate(SimpleNamespace(job_publish_flow_enabled=True, job_publish_kill_switch=False, job_publish_rollout_percentage=25))["ready"]
    assert not evaluate_gate(SimpleNamespace(job_publish_flow_enabled=True, job_publish_kill_switch=True, job_publish_rollout_percentage=100))["ready"]
    assert [step["key"] for step in rollback_plan()] == [
        "job_publish_kill_switch", "action_execution_mode", "job_publish_flow", "domain_outbox_consumer",
    ]
