"""Enterprise WeCom identity-app client.

This client is deliberately separate from both the legacy ``WeComClient`` and
the AIBot WebSocket client.  It never exposes or persists access tokens and it
maps ``open_userid`` values by the response object's keys, never by position.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from app.config import settings

IDENTITY_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
MAX_BATCH_SIZE = 1000
_TEMPORARY_ERRCODES = frozenset({42001, 45009, 50001})
_PERMANENT_ERRCODES = frozenset({40001, 40003, 40013, 40014, 60111, 60112, 40031})


class IdentityClientError(RuntimeError):
    def __init__(self, message: str, *, code: str = "identity_client_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ConversionResult:
    mapping: dict[str, str]
    invalid: frozenset[str]
    batches: int


def _errcode(payload: dict[str, Any], prefix: str) -> int:
    """Validate provider errcode without coercing malformed values."""
    if "errcode" not in payload:
        raise IdentityClientError(f"{prefix} response missing errcode", code=f"{prefix}_invalid", retryable=False)
    value = payload.get("errcode")
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdentityClientError(f"{prefix} response errcode invalid", code=f"{prefix}_invalid", retryable=False)
    if value < 0:
        raise IdentityClientError(f"{prefix} response errcode unknown", code="unknown_errcode", retryable=False)
    return value


def _provider_error(prefix: str, errcode: int) -> IdentityClientError:
    # WeCom may return any documented 5xx code (including 50002) during a
    # transient provider outage.  Treat the complete valid 500-599 range as
    # retryable while keeping unknown non-5xx codes terminal.
    if errcode in _TEMPORARY_ERRCODES or 500 <= errcode <= 599 or 50000 <= errcode <= 59999:
        return IdentityClientError(f"{prefix} temporary error", code=f"{prefix}_err_{errcode}", retryable=True)
    if errcode in _PERMANENT_ERRCODES:
        return IdentityClientError(f"{prefix} permanent error", code=f"{prefix}_err_{errcode}", retryable=False)
    return IdentityClientError(f"{prefix} unknown error", code="unknown_errcode", retryable=False)


class WeComIdentityAppClient:
    """Small, injectable HTTP client for the identity application."""

    def __init__(
        self,
        corp_id: str | None = None,
        app_secret: str | None = None,
        *,
        timeout: float = 10.0,
        transport: Any | None = None,
        http_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        secret = app_secret
        if secret is None:
            secret = settings.wecom_aibot_identity_app_secret.get_secret_value()
        self.corp_id = (corp_id if corp_id is not None else settings.wecom_corp_id).strip()
        self._app_secret = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret or "")
        if not self.corp_id or not self._app_secret:
            raise ValueError("corp_id and identity app secret are required")
        self.timeout = timeout
        self._transport = transport or http_client or httpx
        self._clock = clock
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    def get_access_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token_expires_at:
                return self._token
            try:
                response = self._transport.get(
                    f"{IDENTITY_API_BASE}/gettoken",
                    params={"corpid": self.corp_id, "corpsecret": self._app_secret},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise IdentityClientError("identity app token request failed", code="token_unavailable", retryable=True) from exc
            if not isinstance(payload, dict):
                raise IdentityClientError("identity app token response invalid", code="token_invalid", retryable=True)
            errcode = _errcode(payload, "token")
            token = payload.get("access_token")
            if errcode != 0:
                raise _provider_error("token", errcode)
            if not isinstance(token, str) or not token:
                raise IdentityClientError("identity app token missing", code="token_invalid", retryable=False)
            expires = payload.get("expires_in", 7200)
            try:
                expires_seconds = max(60, int(expires))
            except (TypeError, ValueError):
                expires_seconds = 7200
            self._token = token
            self._token_expires_at = self._clock() + expires_seconds - 300
            return token

    def invalidate_token(self) -> None:
        with self._lock:
            self._token = ""
            self._token_expires_at = 0.0

    def batch_openuserid_to_userid(self, open_userids: list[str] | tuple[str, ...] | set[str]) -> ConversionResult:
        values: list[str] = []
        seen: set[str] = set()
        for value in open_userids:
            if not isinstance(value, str) or not value or len(value) > 128:
                raise IdentityClientError("invalid open_userid input", code="invalid_open_userid", retryable=False)
            if value not in seen:
                seen.add(value)
                values.append(value)
        if not values:
            return ConversionResult({}, frozenset(), 0)

        mapping: dict[str, str] = {}
        invalid: set[str] = set()
        batches = 0
        for offset in range(0, len(values), MAX_BATCH_SIZE):
            chunk = values[offset : offset + MAX_BATCH_SIZE]
            result = self._convert_batch(chunk)
            mapping.update(result[0])
            invalid.update(result[1])
            batches += 1
        unresolved = set(values) - set(mapping) - invalid
        if unresolved:
            raise IdentityClientError("identity conversion response incomplete", code="conversion_incomplete", retryable=True)
        return ConversionResult(mapping, frozenset(invalid), batches)

    def is_canonical_user_visible(self, userid: str) -> tuple[bool, str]:
        """Check directory visibility without returning member/PII fields.

        The endpoint response is reduced to a boolean and a stable reason
        code.  Callers must not log or persist the response body.
        """
        if not isinstance(userid, str) or not userid or len(userid) > 64:
            return False, "invalid_canonical_userid"
        try:
            token = self.get_access_token()
        except IdentityClientError as exc:
            return False, "directory_unavailable" if exc.retryable else "directory_auth_failed"
        try:
            response = self._transport.get(
                f"{IDENTITY_API_BASE}/user/get",
                params={"access_token": token, "userid": userid},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return False, "directory_unavailable"
        if not isinstance(payload, dict):
            return False, "directory_invalid_response"
        try:
            errcode = _errcode(payload, "directory")
        except IdentityClientError as exc:
            return False, exc.code
        if errcode == 0:
            # Do not expose/return payload fields such as name, mobile or
            # department; errcode=0 is the only visibility signal required.
            return True, "visible"
        if errcode in {60111, 60112, 40031}:
            return False, "directory_not_visible"
        if 500 <= errcode <= 599 or 50000 <= errcode <= 59999:
            return False, "directory_unavailable"
        try:
            raise _provider_error("directory", errcode)
        except IdentityClientError as exc:
            return False, "directory_unavailable" if exc.retryable else exc.code

    def _convert_batch(self, chunk: list[str]) -> tuple[dict[str, str], set[str]]:
        token = self.get_access_token()
        try:
            response = self._transport.post(
                f"{IDENTITY_API_BASE}/batch/openuserid_to_userid",
                params={"access_token": token},
                json={"open_userid_list": chunk},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise IdentityClientError("identity conversion request failed", code="conversion_unavailable", retryable=True) from exc
        if not isinstance(payload, dict):
            raise IdentityClientError("identity conversion response invalid", code="conversion_invalid", retryable=True)
        errcode = _errcode(payload, "conversion")
        if errcode != 0:
            raise _provider_error("conversion", errcode)
        rows = payload.get("userid_list", [])
        bad = payload.get("invalid_open_userid_list", [])
        if not isinstance(rows, list) or not isinstance(bad, list) or any(not isinstance(v, str) for v in bad):
            raise IdentityClientError("identity conversion lists invalid", code="conversion_invalid", retryable=True)
        input_set = set(chunk)
        mapping: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise IdentityClientError("identity conversion item invalid", code="conversion_invalid", retryable=True)
            source, userid = row.get("open_userid"), row.get("userid")
            if not isinstance(source, str) or not isinstance(userid, str) or source not in input_set or not userid or len(userid) > 64:
                raise IdentityClientError("identity conversion mapping invalid", code="conversion_invalid", retryable=True)
            if source in mapping:
                raise IdentityClientError("identity conversion mapping duplicated", code="conversion_invalid", retryable=True)
            mapping[source] = userid
        invalid = set(bad)
        if not invalid.issubset(input_set) or set(mapping) & invalid:
            raise IdentityClientError("identity conversion response does not match request", code="conversion_invalid", retryable=True)
        return mapping, invalid


IdentityAppClient = WeComIdentityAppClient
AibotIdentityAppClient = WeComIdentityAppClient


def batch_openuserid_to_userid(client: WeComIdentityAppClient, open_userids: list[str]) -> ConversionResult:
    """Functional alias useful for dependency-injected workers/tests."""
    return client.batch_openuserid_to_userid(open_userids)
