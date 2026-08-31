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
            errcode = int(payload.get("errcode", 0) or 0)
            token = payload.get("access_token")
            if errcode != 0 or not isinstance(token, str) or not token:
                raise IdentityClientError("identity app token rejected", code=f"token_err_{errcode}", retryable=errcode in {40001, 40014, 42001, 45009})
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
        try:
            errcode = int(payload.get("errcode", 0) or 0)
        except (TypeError, ValueError):
            raise IdentityClientError("identity conversion errcode invalid", code="conversion_invalid", retryable=True)
        if errcode != 0:
            raise IdentityClientError("identity conversion rejected", code=f"conversion_err_{errcode}", retryable=errcode >= 500 or errcode in {40014, 42001, 45009})
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
