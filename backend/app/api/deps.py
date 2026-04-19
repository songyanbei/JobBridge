"""共享依赖（Phase 5）。

- get_db: 数据库会话
- get_redis_dep: Redis 客户端
- require_admin: JWT 鉴权，返回 AdminUser ORM
- require_event_api_key: 事件回传 API Key 校验（不走 JWT）
"""
from __future__ import annotations

from fastapi import Depends, Header
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import BusinessException
from app.core.redis_client import get_redis
from app.core.security import decode_admin_token
from app.db import SessionLocal
from app.models import AdminUser

# OAuth2 Bearer，auto_error=False 让我们自己抛统一错误码
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/admin/login", auto_error=False)


def get_db():
    """DB 会话依赖，请求结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_redis_dep():
    """Redis 客户端依赖。"""
    return get_redis()


def require_admin(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    """JWT 鉴权中间件。

    - 无 token → 40003
    - token 过期 → 40002（decode_admin_token 会抛出）
    - token 无效 / 用户被禁用 / 不存在 → 40003

    注意：本依赖**不**校验 `password_changed`。需要强制首登改密的业务路由请改用
    `require_admin_password_changed`，保留本依赖给 `/admin/me` 与
    `/admin/me/password` 这类必须放行的端点。
    """
    if not token:
        raise BusinessException(40003, "Token 无效")

    claims = decode_admin_token(token)  # 过期/无效会抛 40002/40003
    try:
        admin_id = int(claims.get("sub", "0"))
    except (TypeError, ValueError) as exc:
        raise BusinessException(40003, "Token 无效") from exc

    admin = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not admin or not admin.enabled:
        raise BusinessException(40003, "Token 无效")
    return admin


def require_admin_password_changed(
    current: AdminUser = Depends(require_admin),
) -> AdminUser:
    """在 ``require_admin`` 基础上叠加"必须已改默认密码"门禁。

    - 开关由 ``settings.admin_force_password_change`` 控制（默认 True）。
    - 命中拒绝时返回 40301 + 文案 "请先修改默认密码"，前端据此跳改密页。
    - 仅对业务接口生效；``/admin/me`` 与 ``/admin/me/password`` 仍走 ``require_admin``。

    Why:
        seed.sql 默认 admin/admin123 + ``password_changed=0``。Phase 7 上线
        checklist 要求改密；但运维忘改时业务接口必须自我保护，避免默认口令
        被外网扫到后直接撞库放行。
    """
    if settings.admin_force_password_change and not bool(current.password_changed):
        raise BusinessException(40301, "请先修改默认密码")
    return current


def require_event_api_key(
    x_event_api_key: str | None = Header(default=None, alias="X-Event-Api-Key"),
) -> None:
    """校验 X-Event-Api-Key Header。

    注意：事件回传接口独立于 JWT 鉴权体系，供外部可信系统（如小程序后端）调用。
    """
    expected = settings.event_api_key
    if not expected:
        # 未配置 API Key 时直接拒绝，避免裸奔
        raise BusinessException(40001, "Invalid API Key")
    if x_event_api_key != expected:
        raise BusinessException(40001, "Invalid API Key")
