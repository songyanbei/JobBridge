"""企微 API 客户端封装。

封装 access token 获取、消息发送、素材下载、外部联系人查询等基础 HTTP 调用。
业务层只调用方法，不允许手拼 URL。
"""
import logging
import time
import threading

import httpx

from app.config import settings
from app.core.exceptions import AppError

logger = logging.getLogger(__name__)

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComError(AppError):
    """企微 API 调用失败。"""
    def __init__(self, message: str, errcode: int = 0):
        self.errcode = errcode
        super().__init__(message, "WECOM_ERROR")


class WeComClient:
    """企业微信 API 客户端。

    封装 token 管理和基础 HTTP 调用，外部只调方法不接触 URL。
    """

    def __init__(
        self,
        corp_id: str | None = None,
        secret: str | None = None,
        agent_id: str | None = None,
        timeout: int = 10,
    ):
        self._corp_id = corp_id or settings.wecom_corp_id
        self._secret = secret or settings.wecom_secret
        # Distinguish "caller explicitly passed empty/0 agent" from "caller
        # didn't pass one". `or` treats "" as falsy and silently falls back to
        # the default config, which broke test_empty_agent_id_defaults_to_zero.
        raw_agent_id = settings.wecom_agent_id if agent_id is None else agent_id
        if raw_agent_id and not raw_agent_id.isdigit():
            raise ValueError(
                f"wecom_agent_id must be a numeric string, got: '{raw_agent_id}'"
            )
        self._agent_id = int(raw_agent_id) if raw_agent_id else 0
        self._timeout = timeout

        self._access_token: str = ""
        self._token_expires_at: float = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Token 管理
    # ------------------------------------------------------------------

    def _refresh_token(self) -> str:
        """从企微 API 获取 access_token。"""
        url = f"{WECOM_API_BASE}/gettoken"
        params = {"corpid": self._corp_id, "corpsecret": self._secret}

        resp = httpx.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()

        errcode = data.get("errcode", 0)
        if errcode != 0:
            raise WeComError(
                f"Failed to get access_token: {data.get('errmsg', 'unknown')}",
                errcode=errcode,
            )

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 7200) - 300
        return self._access_token

    def get_access_token(self) -> str:
        """获取有效的 access_token，过期自动刷新。"""
        with self._lock:
            if self._access_token and time.time() < self._token_expires_at:
                return self._access_token
            return self._refresh_token()

    def invalidate_token(self) -> None:
        """使本地缓存的 access_token 立即失效。

        调用场景：
        - send_text 等接口返回 errcode=42001（access_token expired），
          业务方需要强制刷新后再重试一次。
        持锁写入 _access_token / _token_expires_at 保证线程安全。
        """
        with self._lock:
            self._access_token = ""
            self._token_expires_at = 0

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------

    def send_text(self, to_user: str, content: str) -> dict:
        """向指定用户发送文本消息。

        Args:
            to_user: 接收者的 external_userid 或 userid
            content: 文本内容

        Returns:
            企微 API 响应字典。

        Raises:
            WeComError: API 调用失败。
        """
        # ═══════════════════════════════════════════════════════════
        # [MOCK-WEWORK] BEGIN — 接入真企微时删除此整块（含 BEGIN/END 注释）
        # 说明：演示期间拦截出站消息投给 mock-testbed 的 SSE 通道。
        # 依赖：环境变量 MOCK_WEWORK_OUTBOUND=true。
        # 完整删除指南：mock-testbed/README.md §删除指南
        import os as _mw_os  # noqa: E402
        if _mw_os.environ.get("MOCK_WEWORK_OUTBOUND", "").lower() == "true":
            # 生产环境硬守卫：防止 env var 被误带进 prod 导致所有出站消息静默吞掉
            if _mw_os.environ.get("APP_ENV", "").lower() == "production":
                raise RuntimeError(
                    "[MOCK-WEWORK] refuses to activate in production "
                    "(APP_ENV=production detected). "
                    "Unset MOCK_WEWORK_OUTBOUND in production env or change APP_ENV."
                )
            import json as _mw_json  # noqa: E402
            import secrets as _mw_secrets  # noqa: E402
            import redis as _mw_redis  # noqa: E402
            logger.warning(
                "[MOCK-WEWORK] short-circuit send_text to_user=%s len=%d",
                to_user, len(content or ""),
            )
            _mw_payload = {
                "touser": to_user,
                "msgtype": "text",
                "agentid": self._agent_id,
                "text": {"content": content},
            }
            try:
                _mw_r = _mw_redis.Redis.from_url(
                    _mw_os.environ.get("MOCK_WEWORK_REDIS_URL", "redis://localhost:6379/0"),
                    decode_responses=True,
                )
                _mw_r.publish(
                    f"mock:outbound:{to_user}",
                    _mw_json.dumps(_mw_payload, ensure_ascii=False),
                )
            except Exception:  # noqa: BLE001
                logger.exception("[MOCK-WEWORK] publish to mock bus failed; swallow and return ok")
            # 返回体字段和真企微 /cgi-bin/message/send 成功响应对齐（partial-failure 字段为空）
            return {
                "errcode": 0,
                "errmsg": "ok",
                "invaliduser": "",
                "invalidparty": "",
                "invalidtag": "",
                "unlicenseduser": "",
                "msgid": f"mock_{_mw_secrets.token_hex(8)}",
                "response_code": "",
            }
        # [MOCK-WEWORK] END
        # ═══════════════════════════════════════════════════════════

        url = f"{WECOM_API_BASE}/message/send"
        token = self.get_access_token()

        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": self._agent_id,
            "text": {"content": content},
        }

        resp = httpx.post(
            url,
            params={"access_token": token},
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        errcode = data.get("errcode", 0)
        if errcode != 0:
            raise WeComError(
                f"send_text failed: {data.get('errmsg', 'unknown')}",
                errcode=errcode,
            )

        return data

    def send_text_to_group(self, chat_id: str, content: str) -> bool:
        """向指定企微群聊发送文本消息（Phase 7 §3.1 模块 H）。

        接口：``cgi-bin/appchat/send``（应用群聊消息推送）。

        与 :py:meth:`send_text` 的差异：
        - 失败不抛异常，仅返回 False，便于上层把失败消息改写入 `queue:group_send_retry` 独立重试；
        - token 过期（42001）时触发一次本地 token 失效 + 重试。

        Args:
            chat_id: 企微群 chatid（应用创建的群聊 ID）。
            content: 文本内容。

        Returns:
            True: errcode=0 推送成功；False: 推送失败（已 loguru 记录）。
        """
        if not chat_id:
            logger.warning("send_text_to_group: empty chat_id, skip")
            return False

        # ═══════════════════════════════════════════════════════════
        # [MOCK-WEWORK] BEGIN — 接入真企微时删除此整块（含 BEGIN/END 注释）
        # 说明：演示期间拦截群消息出站投给 mock-testbed 的 SSE 通道。
        # 依赖：环境变量 MOCK_WEWORK_OUTBOUND=true。
        import os as _mw_os  # noqa: E402
        if _mw_os.environ.get("MOCK_WEWORK_OUTBOUND", "").lower() == "true":
            # 生产环境硬守卫（与 send_text 的分支一致）
            if _mw_os.environ.get("APP_ENV", "").lower() == "production":
                raise RuntimeError(
                    "[MOCK-WEWORK] refuses to activate in production "
                    "(APP_ENV=production detected). "
                    "Unset MOCK_WEWORK_OUTBOUND in production env or change APP_ENV."
                )
            import json as _mw_json  # noqa: E402
            import redis as _mw_redis  # noqa: E402
            logger.warning(
                "[MOCK-WEWORK] short-circuit send_text_to_group chat_id=%s len=%d",
                chat_id, len(content or ""),
            )
            _mw_payload = {
                "chatid": chat_id,
                "msgtype": "text",
                "text": {"content": content},
                "safe": 0,
            }
            try:
                _mw_r = _mw_redis.Redis.from_url(
                    _mw_os.environ.get("MOCK_WEWORK_REDIS_URL", "redis://localhost:6379/0"),
                    decode_responses=True,
                )
                _mw_r.publish(
                    f"mock:outbound:chat:{chat_id}",
                    _mw_json.dumps(_mw_payload, ensure_ascii=False),
                )
            except Exception:  # noqa: BLE001
                logger.exception("[MOCK-WEWORK] publish group msg to mock bus failed; swallow and return True")
            return True
        # [MOCK-WEWORK] END
        # ═══════════════════════════════════════════════════════════

        payload = {
            "chatid": chat_id,
            "msgtype": "text",
            "text": {"content": content},
            "safe": 0,
        }

        try:
            token = self.get_access_token()
            resp = httpx.post(
                f"{WECOM_API_BASE}/appchat/send",
                params={"access_token": token},
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            errcode = data.get("errcode", -1)
            if errcode == 0:
                return True

            # token 过期 → 失效并重试一次
            if errcode == 42001:
                self.invalidate_token()
                token = self.get_access_token()
                resp = httpx.post(
                    f"{WECOM_API_BASE}/appchat/send",
                    params={"access_token": token},
                    json=payload,
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("errcode", -1) == 0:
                    return True

            logger.warning(
                "send_text_to_group failed: chat_id=%s errcode=%s errmsg=%s",
                chat_id, data.get("errcode"), data.get("errmsg"),
            )
            return False
        except Exception:
            logger.exception("send_text_to_group raised: chat_id=%s", chat_id)
            return False

    # ------------------------------------------------------------------
    # 素材下载
    # ------------------------------------------------------------------

    def download_media(self, media_id: str) -> bytes:
        """下载临时素材（图片、语音等）。

        Args:
            media_id: 企微 media_id

        Returns:
            文件二进制内容。

        Raises:
            WeComError: API 调用失败。
        """
        url = f"{WECOM_API_BASE}/media/get"
        token = self.get_access_token()

        resp = httpx.get(
            url,
            params={"access_token": token, "media_id": media_id},
            timeout=self._timeout,
        )
        resp.raise_for_status()

        # 如果返回 JSON 说明出错了
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type or "text/plain" in content_type:
            data = resp.json()
            raise WeComError(
                f"download_media failed: {data.get('errmsg', 'unknown')}",
                errcode=data.get("errcode", -1),
            )

        return resp.content

    # ------------------------------------------------------------------
    # 外部联系人
    # ------------------------------------------------------------------

    def get_external_contact(self, external_userid: str) -> dict | None:
        """获取外部联系人详情。

        Args:
            external_userid: 外部联系人的 userid

        Returns:
            联系人详情字典；用户不存在或已删除时返回 None。

        Raises:
            WeComError: 网络错误、鉴权失败等非"用户不存在"的异常。
        """
        url = f"{WECOM_API_BASE}/externalcontact/get"
        token = self.get_access_token()

        resp = httpx.get(
            url,
            params={"access_token": token, "external_userid": external_userid},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        errcode = data.get("errcode", 0)

        # 84061: 不存在的external_userid
        # 不存在的用户返回 None
        if errcode in (84061,):
            return None

        if errcode != 0:
            raise WeComError(
                f"get_external_contact failed: {data.get('errmsg', 'unknown')}",
                errcode=errcode,
            )

        return data.get("external_contact", data)
