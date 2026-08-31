"""Channel-aware outbound delivery adapters.

Business services only persist an outbox row.  The adapter selected here owns
the provider-specific send operation, which keeps the legacy HTTP client and
the AIBot WebSocket transport from crossing channel boundaries.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from app.wecom.client import WeComClient

LEGACY_CHANNEL = "wecom_app"
AIBOT_CHANNEL = "wecom_aibot"


class DeliverySender(ABC):
    """Provider-neutral sender contract consumed by Worker/connection code."""

    channel: str

    @abstractmethod
    def send(self, item: Mapping[str, Any]) -> Any:
        """Send one claimed outbox item and return a provider response."""


class LegacyWeComSender(DeliverySender):
    """Traditional self-built WeCom application sender."""

    channel = LEGACY_CHANNEL

    def __init__(self, client: WeComClient | None = None) -> None:
        self.client = client or WeComClient()

    def send(self, item: Mapping[str, Any]) -> dict:
        userid = str(item.get("userid") or "")
        if not userid:
            raise ValueError("legacy WeCom delivery requires userid")
        content = str(item.get("content") or "")
        # Deliberately call only the legacy HTTP client.  AIBot rows are never
        # allowed to reach this adapter through registry lookup.
        return self.client.send_text(userid, content)


class AibotSender(DeliverySender):
    """AIBot sender backed by the active connection transport.

    ``transport`` is intentionally a small injected object rather than a
    WebSocket instance.  The connection service owns socket lifecycle and
    fencing; Worker can therefore never accidentally retain or use a socket.
    The transport may expose either ``send_outbox(item)`` or
    ``send(item)``; both forms are supported to keep the adapter easy to test.
    """

    channel = AIBOT_CHANNEL

    def __init__(self, transport: Any | None = None) -> None:
        self.transport = transport

    def send(self, item: Mapping[str, Any]) -> Any:
        if self.transport is None:
            raise RuntimeError("AIBot connection is not active")
        if hasattr(self.transport, "send_outbox"):
            return self.transport.send_outbox(item)
        send = getattr(self.transport, "send", None)
        if send is None:
            raise TypeError("AIBot transport must expose send_outbox or send")
        return send(item)


class DeliveryRegistry:
    """Explicit channel -> sender registry with fail-closed lookup."""

    def __init__(self, senders: Mapping[str, DeliverySender] | None = None) -> None:
        # AIBot is registered even while disconnected so channel lookup is
        # explicit and fail-closed; it cannot silently fall back to legacy.
        self._senders: dict[str, DeliverySender] = dict(senders or {
            LEGACY_CHANNEL: LegacyWeComSender(),
            AIBOT_CHANNEL: AibotSender(None),
        })

    def register(self, sender: DeliverySender, *, replace: bool = False) -> None:
        if sender.channel in self._senders and not replace:
            raise ValueError(f"sender already registered for channel {sender.channel}")
        self._senders[sender.channel] = sender

    def for_channel(self, channel: str | None) -> DeliverySender:
        """Resolve a sender; unknown/empty channels fail closed."""
        key = str(channel or "").strip()
        try:
            return self._senders[key]
        except KeyError as exc:
            raise ValueError(f"no delivery sender registered for channel {key!r}") from exc

    def channels(self) -> tuple[str, ...]:
        return tuple(sorted(self._senders))


_default_registry: DeliveryRegistry | None = None


def get_delivery_registry(*, aibot_transport: Any | None = None) -> DeliveryRegistry:
    """Return the process registry.

    The legacy adapter is always available.  AIBot is registered only when a
    connection transport is explicitly supplied, so a disabled/disconnected
    connector cannot make Worker silently fall back to the legacy API.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = DeliveryRegistry()
    if aibot_transport is not None:
        _default_registry.register(AibotSender(aibot_transport), replace=True)
    return _default_registry


def for_channel(channel: str | None) -> DeliverySender:
    """Convenience lookup used by delivery loops."""
    return get_delivery_registry().for_channel(channel)


def reset_delivery_registry() -> None:
    """Test hook; production code should inject/replace a transport instead."""
    global _default_registry
    _default_registry = None
