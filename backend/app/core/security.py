"""鉴权与密码安全工具（Phase 5）。

- bcrypt 密码哈希与校验
- Admin JWT 颁发 / 解析

说明：
- bcrypt 通过 `passlib` 统一封装；历史数据与 `seed.sql` 中的 `$2b$10$...`
  哈希可直接兼容（passlib 的 bcrypt scheme 与 `bcrypt` 库互通）。
- JWT 使用 HS256，秘钥来自 `settings.admin_jwt_secret`；一期不做 refresh token。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext

from app.config import settings
from app.core.exceptions import BusinessException

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """使用 bcrypt 哈希明文密码。"""
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配。

    如果 `hashed` 格式异常（例如为空或残缺），返回 False 而不是抛异常，
    方便登录接口按"用户名或密码错误"统一处理。
    """
    if not plain or not hashed:
        return False
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

JWT_ALGORITHM = "HS256"


def create_admin_token(admin_id: int, username: str) -> tuple[str, datetime]:
    """为管理员颁发 JWT，返回 (token, expires_at)。

    载荷：{"sub": <admin_id>, "username": <username>, "exp": <timestamp>}
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.admin_jwt_expires_hours)
    payload: dict[str, Any] = {
        "sub": str(admin_id),
        "username": username,
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.admin_jwt_secret, algorithm=JWT_ALGORITHM)
    return token, expires_at


def decode_admin_token(token: str) -> dict[str, Any]:
    """解析 JWT，过期返回 40002，其它异常返回 40003。"""
    try:
        return jwt.decode(token, settings.admin_jwt_secret, algorithms=[JWT_ALGORITHM])
    except ExpiredSignatureError as exc:
        raise BusinessException(40002, "Token 过期") from exc
    except JWTError as exc:
        raise BusinessException(40003, "Token 无效") from exc
