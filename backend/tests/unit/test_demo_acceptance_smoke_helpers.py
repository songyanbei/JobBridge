"""Unit tests for demo_acceptance_smoke helper functions."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.demo_acceptance_smoke import assert_text, build_inbound_payload


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
