import logging
from types import SimpleNamespace

import pytest

from app.services import lifecycle_config_service
from app.services.lifecycle_config_service import (
    get_hard_delete_delay_days,
    get_job_candidate_ttl_days,
    get_job_ttl_days,
)


@pytest.fixture(autouse=True)
def _reset_missing_warning_state():
    lifecycle_config_service._missing_warning_last_at.clear()
    yield
    lifecycle_config_service._missing_warning_last_at.clear()


class Query:
    def __init__(self, value): self.value = value
    def filter(self, *args): return self
    def first(self): return SimpleNamespace(config_value=self.value)
class DB:
    def __init__(self, value): self.value = value
    def query(self, *args): return Query(self.value)

def test_ttl_ranges_fallback_to_safe_defaults():
    assert get_job_ttl_days(DB("0")) == 30
    assert get_job_candidate_ttl_days(DB("366")) == 7


def test_hard_delete_delay_accepts_zero_and_rejects_out_of_range_values():
    assert get_hard_delete_delay_days(DB("0")) == 0
    assert get_hard_delete_delay_days(DB("3650")) == 3650
    assert get_hard_delete_delay_days(DB("-1")) == 7
    assert get_hard_delete_delay_days(DB("3651")) == 7
    assert get_hard_delete_delay_days(DB("invalid")) == 7


class MissingQuery:
    def filter(self, *args):
        return self

    def first(self):
        return None


class MissingDB:
    def query(self, *args):
        return MissingQuery()


@pytest.mark.parametrize(
    ("reader", "key", "fallback"),
    [
        (get_job_ttl_days, "ttl.job.days", 30),
        (get_job_candidate_ttl_days, "ttl.job.candidate.days", 7),
        (get_hard_delete_delay_days, "ttl.hard_delete.delay_days", 7),
    ],
)
def test_missing_config_warns_with_key_raw_value_and_fallback(
    reader,
    key,
    fallback,
    caplog,
):
    with caplog.at_level(logging.WARNING, logger=lifecycle_config_service.logger.name):
        assert reader(MissingDB()) == fallback

    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == (
        f"missing_lifecycle_config key={key} value=None fallback={fallback}"
    )


def test_invalid_config_warning_keeps_raw_and_fallback(caplog):
    with caplog.at_level(logging.WARNING, logger=lifecycle_config_service.logger.name):
        assert get_job_ttl_days(DB("not-an-int")) == 30

    assert caplog.records[0].getMessage() == (
        "invalid_lifecycle_config key=ttl.job.days "
        "value='not-an-int' fallback=30"
    )


def test_null_config_value_is_invalid_not_missing(caplog):
    with caplog.at_level(logging.WARNING, logger=lifecycle_config_service.logger.name):
        assert get_job_ttl_days(DB(None)) == 30

    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == (
        "invalid_lifecycle_config key=ttl.job.days value=None fallback=30"
    )


def test_valid_config_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger=lifecycle_config_service.logger.name):
        assert get_job_ttl_days(DB("3650")) == 3650

    assert caplog.records == []


def test_missing_config_warning_is_rate_limited_per_key(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(lifecycle_config_service, "monotonic", lambda: now[0])
    with caplog.at_level(logging.WARNING, logger=lifecycle_config_service.logger.name):
        get_job_ttl_days(MissingDB())
        get_job_ttl_days(MissingDB())
        get_job_candidate_ttl_days(MissingDB())
        now[0] += lifecycle_config_service.MISSING_CONFIG_WARNING_INTERVAL_SECONDS
        get_job_ttl_days(MissingDB())

    messages = [record.getMessage() for record in caplog.records]
    assert sum("key=ttl.job.days " in message for message in messages) == 2
    assert sum("key=ttl.job.candidate.days " in message for message in messages) == 1


def test_recovered_config_can_warn_immediately_when_missing_again(monkeypatch, caplog):
    monkeypatch.setattr(lifecycle_config_service, "monotonic", lambda: 1000.0)
    with caplog.at_level(logging.WARNING, logger=lifecycle_config_service.logger.name):
        get_job_ttl_days(MissingDB())
        get_job_ttl_days(DB("30"))
        get_job_ttl_days(MissingDB())

    assert len(caplog.records) == 2
    assert all(
        record.getMessage()
        == "missing_lifecycle_config key=ttl.job.days value=None fallback=30"
        for record in caplog.records
    )
