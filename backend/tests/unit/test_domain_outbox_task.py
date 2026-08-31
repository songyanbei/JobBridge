from app.tasks import domain_outbox_consumer


def test_consumer_task_is_fail_closed_when_disabled(monkeypatch):
    monkeypatch.setattr(domain_outbox_consumer.settings, "domain_outbox_consumer_enabled", False)
    assert domain_outbox_consumer.run_once(lambda _: None)["claimed"] == 0
    assert domain_outbox_consumer.health_snapshot()["healthy"] is True


def test_default_handler_is_available():
    assert callable(domain_outbox_consumer.default_handler)
