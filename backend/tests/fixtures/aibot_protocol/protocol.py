"""Load and validate the frozen minimum AIBot JSON protocol fixtures.

The strings in ``*.json`` intentionally stay byte-for-byte close to the
architecture document.  Tests should use ``load_fixture`` rather than repeat
protocol payloads inline, so a protocol change is visible in one place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent

_NAMES = (
    "aibot_subscribe",
    "subscribe_ack",
    "aibot_msg_callback",
    "aibot_event_callback",
    "aibot_respond_welcome_msg",
    "aibot_respond_msg",
    "aibot_respond_update_msg",
    "aibot_send_msg",
)


def fixture_names() -> tuple[str, ...]:
    """Return the names of all frozen protocol fixtures."""

    return _NAMES


def load_fixture(name: str) -> dict[str, Any]:
    """Read a fixture as a fresh JSON object.

    A fresh decode on every call prevents one test mutating another test's
    payload and ensures that the fixture is valid JSON at collection time.
    """

    if name not in _NAMES:
        raise KeyError(f"unknown AIBot fixture: {name}")
    path = FIXTURE_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_json(name: str) -> str:
    """Return the exact fixture text, useful for transport send assertions."""

    if name not in _NAMES:
        raise KeyError(f"unknown AIBot fixture: {name}")
    return (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8").strip()


def assert_protocol_fixture(name: str, payload: dict[str, Any] | None = None) -> None:
    """Assert the protocol-critical fields common to the official examples."""

    value = load_fixture(name) if payload is None else payload
    assert isinstance(value, dict)
    if name == "subscribe_ack":
        assert value["headers"]["req_id"] == "sub-1"
        assert value["errcode"] == 0
        assert value["errmsg"] == "ok"
        return

    assert value["cmd"].startswith("aibot_")
    assert value["headers"]["req_id"]
    body = value.get("body") or {}
    if name in {"aibot_msg_callback", "aibot_event_callback"}:
        assert body["msgid"]
        assert body["aibotid"] == "AIBOTID"
        assert body["chattype"] in {"single", "group"}
        assert body["from"]["userid"] == "USERID"
        assert body["msgtype"]
    if name == "aibot_respond_update_msg":
        assert body["stream"]["id"] == "STREAMID"
        assert body["stream"]["finish"] is False

