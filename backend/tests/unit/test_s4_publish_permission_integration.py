from types import SimpleNamespace

from app.services.permission_service import can_publish_job


def test_publish_permission_fails_closed_when_status_missing():
    assert not can_publish_job(SimpleNamespace(role="factory", external_userid="f1"))
    assert not can_publish_job(SimpleNamespace(role="broker", external_userid="b1", can_search_workers=True))


def test_publish_permission_requires_active_owner_and_broker_capability():
    factory = SimpleNamespace(role="factory", status="active", external_userid="f1")
    broker = SimpleNamespace(role="broker", status="active", external_userid="b1", can_search_workers=True)
    assert can_publish_job(factory, owner_userid="f1")
    assert not can_publish_job(factory, owner_userid="other")
    assert can_publish_job(broker, owner_userid="b1")
    broker.can_search_workers = False
    assert not can_publish_job(broker, owner_userid="b1")
