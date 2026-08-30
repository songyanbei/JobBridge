"""Unit tests for demo_acceptance_smoke helper functions."""
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.demo_acceptance_smoke import (
    assert_text,
    build_inbound_payload,
    check_mysql_seed,
    cleanup_mysql_smoke_data,
    cleanup_redis_smoke_data,
    create_mysql_inbound_event,
    parse_args,
    wait_for_worker_done,
)


def test_build_inbound_payload_shape():
    payload = build_inbound_payload("phase5_user", "找苏州电子厂")

    assert payload["from_userid"] == "phase5_user"
    assert payload["msg_type"] == "text"
    assert payload["content"] == "找苏州电子厂"
    assert payload["msg_id"].startswith("phase5_smoke_")
    assert payload["media_id"] is None
    assert payload["inbound_event_id"] is None


def test_assert_text_checks_expect_and_reject():
    payloads = [{"text": {"content": "为您找到 3 个匹配岗位\n匹配依据：地点符合 苏州市"}}]

    assert_text(payloads, ["匹配依据"], ["身份证"])

    with pytest.raises(AssertionError):
        assert_text(payloads, ["不存在"], [])

    with pytest.raises(AssertionError):
        assert_text(payloads, [], ["匹配依据"])


def test_parse_args_supports_warmup_and_multiple_messages():
    args = parse_args([
        "--base-url", "http://127.0.0.1:8000",
        "--redis-url", "redis://127.0.0.1:6379/0",
        "--message", "找岗位",
        "--message", "更多",
    ])

    assert args.no_warmup is False
    assert args.seed_version == "demo_supp_v1"
    assert args.min_supplement_jobs == 7
    assert args.min_step5_jobs == 2
    assert args.min_step6_jobs == 2
    assert args.min_step8_jobs == 3
    assert args.skip_seed_check is False
    assert args.warmup_message == "你好"
    assert args.seed_city_like == "苏州"
    assert args.seed_job_category_like == "电子"
    assert args.keep_session is False
    assert args.message == ["找岗位", "更多"]


def test_check_mysql_seed_uses_job_table_and_seed_filters(monkeypatch):
    calls = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, query, params):
            calls["query"] = query
            calls["full_params"] = params
            calls["params"] = params[-2:]

        def fetchone(self):
            return {
                "passed_jobs": 2,
                "supplement_jobs": 7,
                "step5_jobs": 2,
                "step6_jobs": 2,
                "step8_jobs": 3,
            }

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            calls["closed"] = True

    fake_pymysql = SimpleNamespace(
        connect=lambda **kwargs: calls.setdefault("connect", kwargs) and FakeConnection(),
        cursors=SimpleNamespace(DictCursor=object),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    result = check_mysql_seed(
        "mysql+pymysql://user:pass@127.0.0.1:3306/jobbridge?charset=utf8mb4",
        1,
        city_like="苏州",
        job_category_like="电子",
    )

    assert result["checked"] is True
    assert result["seed_version"] == "demo_supp_v1"
    assert result["supplement_jobs"] == 7
    assert result["scenario_counts"] == {"5": 2, "6": 2, "8": 3}
    assert calls["full_params"][:4] == ("demo_supp_v1",) * 4
    assert "FROM job" in calls["query"]
    assert calls["params"] == ("%苏州%", "%电子%")
    assert calls["closed"] is True


def test_check_mysql_seed_requires_dsn():
    with pytest.raises(RuntimeError, match="--mysql-dsn is required"):
        check_mysql_seed(
            "",
            1,
            city_like="鑻忓窞",
            job_category_like="鐢靛瓙",
        )


def test_cleanup_redis_smoke_data_removes_only_target_user():
    target = '{"from_userid":"smoke-a","msg_id":"m-a"}'
    other = '{"from_userid":"smoke-b","msg_id":"m-b"}'

    class FakeRedis:
        def __init__(self):
            self.items = [target.encode(), other.encode()]
            self.deleted = []

        def lrange(self, *_args):
            return list(self.items)

        def lrem(self, _key, _count, value):
            self.items = [item for item in self.items if item != value]
            return 1

        def delete(self, key):
            self.deleted.append(key)
            return 1

    redis_client = FakeRedis()
    result = cleanup_redis_smoke_data(redis_client, "smoke-a", ["m-a"])

    assert result == {"session_deleted": True, "queue_items_deleted": 1}
    assert redis_client.items == [other.encode()]
    assert redis_client.deleted == ["session:smoke-a"]


def test_cleanup_mysql_smoke_data_deletes_user_owned_rows(monkeypatch):
    calls = {"statements": []}

    class FakeCursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, statement, params):
            calls["statements"].append((statement, params))

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            calls["committed"] = True

        def close(self):
            calls["closed"] = True

    fake_pymysql = SimpleNamespace(
        connect=lambda **_kwargs: FakeConnection(),
        cursors=SimpleNamespace(DictCursor=object),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    result = cleanup_mysql_smoke_data(
        "mysql+pymysql://user:pass@127.0.0.1:3306/jobbridge",
        "smoke-a",
        ["m-a"],
    )

    assert result["checked"] is True
    assert set(result["deleted"]) == {
        "wecom_outbound_outbox", "conversation_log", "wecom_inbound_event",
        "contact_access_audit", "contact_delivery", "contact_grant", "contact_request", "audit_log",
        "resume", "job", "user",
    }
    assert len(calls["statements"]) == 11
    assert calls["committed"] is True
    assert calls["closed"] is True


def test_create_mysql_inbound_event_returns_id_and_commits(monkeypatch):
    calls = {}

    class FakeCursor:
        lastrowid = 42

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, statement, params):
            calls["statement"] = statement
            calls["params"] = params

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            calls["committed"] = True

        def close(self):
            calls["closed"] = True

    fake_pymysql = SimpleNamespace(
        connect=lambda **_kwargs: FakeConnection(),
        cursors=SimpleNamespace(DictCursor=object),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)
    payload = build_inbound_payload("smoke-a", "hello")

    assert create_mysql_inbound_event(
        "mysql+pymysql://user:pass@127.0.0.1:3306/jobbridge",
        payload,
    ) == 42
    assert "INSERT INTO wecom_inbound_event" in calls["statement"]
    assert calls["params"][:3] == (payload["msg_id"], payload["turn_id"], "smoke-a")
    assert calls["committed"] is True
    assert calls["closed"] is True


def test_wait_for_worker_done_collects_all_replies_before_returning(monkeypatch):
    calls = {"statuses": 0}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, _statement, _params):
            calls["statuses"] += 1

        def fetchone(self):
            return {"status": "processing" if calls["statuses"] == 1 else "done"}

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            calls["closed"] = True

    class FakePubSub:
        def __init__(self):
            self.messages = [
                {"type": "message", "data": '{"text":{"content":"first"}}'},
                None,
                {"type": "message", "data": '{"text":{"content":"second"}}'},
            ]

        def get_message(self, timeout):
            del timeout
            if self.messages:
                return self.messages.pop(0)
            return None

    fake_pymysql = SimpleNamespace(
        connect=lambda **_kwargs: FakeConnection(),
        cursors=SimpleNamespace(DictCursor=object),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    payloads = wait_for_worker_done(
        FakePubSub(),
        "mysql+pymysql://user:pass@127.0.0.1:3306/jobbridge",
        "msg-1",
        1,
    )

    assert [item["text"]["content"] for item in payloads] == ["first", "second"]
    assert calls["statuses"] == 2
    assert calls["closed"] is True


def test_wait_for_worker_done_rejects_empty_outbound_after_done(monkeypatch):
    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, _statement, _params):
            pass

        def fetchone(self):
            return {"status": "done"}

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    class EmptyPubSub:
        def get_message(self, timeout):
            del timeout
            return None

    fake_pymysql = SimpleNamespace(
        connect=lambda **_kwargs: FakeConnection(),
        cursors=SimpleNamespace(DictCursor=object),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    with pytest.raises(RuntimeError, match="without an outbound business reply"):
        wait_for_worker_done(
            EmptyPubSub(),
            "mysql+pymysql://user:pass@127.0.0.1:3306/jobbridge",
            "msg-empty",
            1,
        )
