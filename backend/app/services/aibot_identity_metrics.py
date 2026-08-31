"""Bounded, privacy-preserving identity metrics.

The production metrics backend is optional; this module provides a stable
counter interface and keeps actor identifiers out of labels.
"""
from __future__ import annotations

from collections import Counter
from threading import Lock

_lock = Lock()
_counters: Counter[tuple[str, str, str]] = Counter()


def record_identity_seen(actor_kind: str) -> None:
    _record("aibot_identity_seen_total", actor_kind, "")


def record_resolution(result: str, reason: str = "") -> None:
    _record("identity_resolution_total", result, reason[:64])


def record_registration(source: str, role: str, status: str) -> None:
    _record("aibot_registration_total", source, f"{role}:{status}"[:64])


def snapshot() -> dict[tuple[str, str, str], int]:
    with _lock:
        return dict(_counters)


def _record(name: str, first: str, second: str) -> None:
    with _lock:
        _counters[(name, first[:32], second)] += 1

