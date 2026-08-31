from app.tasks import domain_outbox_consumer


def test_consumer_task_is_fail_closed_when_disabled(monkeypatch):
    monkeypatch.setattr(domain_outbox_consumer.settings, "domain_outbox_consumer_enabled", False)
    assert domain_outbox_consumer.run_once(lambda _: None)["claimed"] == 0
