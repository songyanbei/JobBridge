"""Message-side adapter for the isolated demo workspace.

This module deliberately contains no database models.  The demo control-plane
service owns workspace/principal lifecycle; this adapter only translates a
verified actor into a business principal and keeps the active role pointer.

The optional control-plane hook is intentionally small so the message path can
be merged before the admin/data-control implementation is deployed.  A
provider may expose these functions in ``app.services.demo_workspace_service``:

``activate_for_actor(db, actor_userid, bot_id, role) -> DemoActorContext``
``resolve_for_actor(db, actor_userid, bot_id, conversation_type, conversation_id)``
``deactivate_for_actor(db, actor_userid, bot_id)``

All hooks are fail-closed.  In particular, this adapter never creates a
synthetic ``User`` row and never changes the real user's role.
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, replace
from typing import Any, Callable

from app.core.redis_client import get_redis

DEMO_ROLES = frozenset({"worker", "factory", "broker"})
ROLE_ALIASES = {
    "求职者": "worker",
    "工人": "worker",
    "worker": "worker",
    "厂家": "factory",
    "工厂": "factory",
    "factory": "factory",
    "中介": "broker",
    "broker": "broker",
}
DEMO_ACTIVE_PREFIX = "demo:active:"
DEFAULT_DEMO_TTL_SECONDS = 30 * 60

DEMO_DISABLED_REPLY = "当前演示模式未启用，请联系管理员开通。"
DEMO_NOT_ALLOWED_REPLY = "当前账号暂未开通演示权限。"
DEMO_DISABLED_WORKSPACE_REPLY = "当前演示空间已下架，暂不能继续体验。"
DEMO_ROLE_USAGE_REPLY = "请输入 /演示 求职者、/演示 厂家 或 /演示 中介。"
DEMO_ENTERED_REPLY = "已进入【{role}】演示模式。您现在可以体验该角色的完整流程。"
DEMO_EXITED_REPLY = "已退出演示模式，已恢复真实账号身份。"


@dataclass(frozen=True)
class DemoActorContext:
    """A turn-scoped separation of real actor and business principal."""

    demo_mode: bool
    demo_id: str
    real_actor_userid: str
    effective_userid: str
    active_role: str
    bot_id: str = ""
    workspace_status: str = "active"
    conversation_type: str = "single"
    conversation_id: str = ""
    actor_digest: str | None = None

    @property
    def reply_userid(self) -> str:
        """The only userid allowed to leave the system for WeCom delivery."""
        return self.real_actor_userid

    @property
    def session_key(self) -> str:
        """Role-scoped Redis key; never overlaps a real user session."""
        conversation_id = self.conversation_id or self.real_actor_userid
        return (
            f"demo:session:{self.demo_id}:{self.conversation_type}:"
            f"{conversation_id}:{self.active_role}"
        )

    def with_conversation(self, conversation_type: str, conversation_id: str) -> "DemoActorContext":
        return replace(
            self,
            conversation_type=conversation_type or "single",
            conversation_id=conversation_id or self.real_actor_userid,
        )

    def is_usable(self) -> bool:
        return (
            self.demo_mode
            and self.active_role in DEMO_ROLES
            and bool(self.demo_id and self.real_actor_userid and self.effective_userid)
            and self.workspace_status == "active"
        )


@dataclass(frozen=True)
class DemoCommandResult:
    handled: bool
    reply_text: str | None = None
    context: DemoActorContext | None = None


def _setting(name: str, default: Any = None) -> Any:
    """Read settings without making this adapter require config changes first."""
    try:
        from app.config import settings

        value = getattr(settings, name, None)
        if value is not None:
            return value
    except Exception:
        pass
    return os.getenv(name.upper(), default)


def demo_mode_enabled() -> bool:
    app_env = str(_setting("app_env", os.getenv("APP_ENV", ""))).strip().lower()
    if app_env not in {"development", "test"}:
        return False
    raw = _setting("demo_mode_enabled", os.getenv("DEMO_MODE_ENABLED", "false"))
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def bot_allowed(bot_id: str) -> bool:
    """Fail closed when an enabled demo mode has no matching bot allowlist."""
    configured = _setting("demo_allowed_bot_ids", "")
    if isinstance(configured, (tuple, list, set, frozenset)):
        allowed = {str(value).strip() for value in configured if str(value).strip()}
    else:
        allowed = {
            value.strip() for value in str(configured or "").split(",") if value.strip()
        }
    return bool(bot_id and bot_id in allowed)


def parse_demo_command(content: str | None) -> tuple[str, str | None] | None:
    """Parse exact demo commands; natural-language text never switches role."""
    text = (content or "").strip()
    if text in {"/退出演示", "/退出demo", "/demo退出"}:
        return "exit", None
    if text in {"/演示", "/demo"}:
        return "help", None
    for prefix in ("/演示 ", "/演示:", "/demo ", "/demo:"):
        if text.startswith(prefix):
            alias = text[len(prefix):].strip().lower()
            role = ROLE_ALIASES.get(alias) or ROLE_ALIASES.get(text[len(prefix):].strip())
            return ("activate", role) if role else ("help", None)
    return None


def active_pointer_key(real_actor_userid: str) -> str:
    return f"{DEMO_ACTIVE_PREFIX}{real_actor_userid}"


def _ttl_seconds() -> int:
    try:
        return max(60, int(_setting("demo_session_ttl_seconds", DEFAULT_DEMO_TTL_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_DEMO_TTL_SECONDS


def save_active_context(context: DemoActorContext) -> None:
    """Persist only the active pointer, not business session state."""
    get_redis().setex(
        active_pointer_key(context.real_actor_userid),
        _ttl_seconds(),
        json.dumps(context.__dict__, ensure_ascii=False),
    )


def load_active_context(
    real_actor_userid: str,
    *,
    conversation_type: str = "single",
    conversation_id: str = "",
) -> DemoActorContext | None:
    raw = get_redis().get(active_pointer_key(real_actor_userid))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        context = DemoActorContext(**data)
    except (TypeError, ValueError, json.JSONDecodeError):
        # A malformed pointer must not route a real message into an unknown
        # business principal.
        get_redis().delete(active_pointer_key(real_actor_userid))
        return None
    return context.with_conversation(conversation_type, conversation_id)


def clear_active_context(real_actor_userid: str) -> None:
    get_redis().delete(active_pointer_key(real_actor_userid))


def _provider() -> Any | None:
    try:
        return importlib.import_module("app.services.demo_workspace_service")
    except (ImportError, ModuleNotFoundError):
        return None


def _provider_call(name: str, *args, **kwargs) -> Any:
    provider = _provider()
    fn = getattr(provider, name, None) if provider is not None else None
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        # The caller turns a provider failure into a deterministic refusal.
        return None


def handle_command(
    content: str | None,
    *,
    db,
    real_actor_userid: str,
    bot_id: str = "",
    conversation_type: str = "single",
    conversation_id: str = "",
) -> DemoCommandResult:
    """Handle demo commands before LLM classification.

    ``handled=False`` means ordinary JobBridge routing should continue.  Demo
    commands are recognized only when the environment gate is open and only
    in a single chat.
    """
    parsed = parse_demo_command(content)
    if parsed is None:
        return DemoCommandResult(False)
    if not demo_mode_enabled():
        return DemoCommandResult(True, DEMO_DISABLED_REPLY)
    if not bot_allowed(bot_id):
        return DemoCommandResult(True, DEMO_NOT_ALLOWED_REPLY)
    if conversation_type != "single":
        return DemoCommandResult(True, DEMO_NOT_ALLOWED_REPLY)

    action, role = parsed
    if action == "help":
        return DemoCommandResult(True, DEMO_ROLE_USAGE_REPLY)
    if action == "exit":
        if _provider() is None:
            return DemoCommandResult(True, DEMO_NOT_ALLOWED_REPLY)
        result = _provider_call(
            "deactivate_for_actor", db, real_actor_userid, bot_id,
        )
        # Provider may return a status, but clearing the pointer is idempotent
        # and safe after either success or an already-disabled workspace.
        clear_active_context(real_actor_userid)
        return DemoCommandResult(True, DEMO_EXITED_REPLY if result is not False else DEMO_NOT_ALLOWED_REPLY)

    context = _provider_call(
        "activate_for_actor", db, real_actor_userid, bot_id, role,
    )
    if not isinstance(context, DemoActorContext):
        return DemoCommandResult(True, DEMO_NOT_ALLOWED_REPLY)
    context = context.with_conversation(conversation_type, conversation_id)
    if not context.is_usable():
        if context.workspace_status != "active":
            return DemoCommandResult(True, DEMO_DISABLED_WORKSPACE_REPLY)
        return DemoCommandResult(True, DEMO_NOT_ALLOWED_REPLY)
    save_active_context(context)
    display = {"worker": "求职者", "factory": "厂家", "broker": "中介"}[context.active_role]
    return DemoCommandResult(True, DEMO_ENTERED_REPLY.format(role=display), context)


def resolve_active_context(
    *,
    db,
    real_actor_userid: str,
    bot_id: str = "",
    conversation_type: str = "single",
    conversation_id: str = "",
) -> DemoActorContext | None:
    if not demo_mode_enabled() or conversation_type != "single":
        return None
    if not bot_allowed(bot_id):
        return None
    # A pointer without the control-plane provider is not an authorization
    # decision. This protects mixed-version deployments from stale Redis keys.
    if _provider() is None:
        return None
    pointer = load_active_context(
        real_actor_userid,
        conversation_type=conversation_type,
        conversation_id=conversation_id,
    )
    if pointer is None:
        return None
    # The control plane remains authoritative for disabled/cleaned workspaces.
    refreshed = _provider_call(
        "resolve_for_actor", db, real_actor_userid, bot_id,
        conversation_type, conversation_id,
    )
    if isinstance(refreshed, DemoActorContext):
        refreshed = refreshed.with_conversation(conversation_type, conversation_id)
        if refreshed.is_usable():
            save_active_context(refreshed)
            return refreshed
        clear_active_context(real_actor_userid)
        return None
    return pointer if pointer.is_usable() else None
