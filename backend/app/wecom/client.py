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
        raw_agent_id = agent_id or settings.wecom_agent_id
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
