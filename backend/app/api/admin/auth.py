"""运营后台鉴权路由（Phase 5 模块 B）。

- POST /admin/login        登录并颁发 JWT
- GET  /admin/me           当前管理员
- PUT  /admin/me/password  修改密码
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.exceptions import BusinessException
from app.core.redis_client import (
    clear_admin_login_fail,
    get_admin_login_fail,
    incr_admin_login_fail,
)
from app.core.responses import ok
from app.core.security import create_admin_token, verify_password
from app.models import AdminUser
from app.schemas.admin import (
    AdminLogin,
    AdminUserRead,
    ChangePasswordRequest,
)
from app.services import admin_user_service

router = APIRouter(prefix="/admin", tags=["admin-auth"])


@router.post("/login", summary="管理员登录")
def login(req: AdminLogin, db: Session = Depends(get_db)):
    """校验账号密码，颁发 JWT。

    - 用户名/密码错误：40001
    - 账号被禁用：40301
    - 连续 ≥ 3 次失败后服务端 sleep 1 秒，缓解暴力破解
    """
    # 连续失败 ≥ 3 次：延迟 1 秒
    try:
        fail_count = get_admin_login_fail(req.username)
    except Exception:
        fail_count = 0
    if fail_count >= 3:
        time.sleep(1)

    admin = admin_user_service.get_by_username(db, req.username)

    if not admin or not verify_password(req.password, admin.password_hash or ""):
        try:
            incr_admin_login_fail(req.username)
        except Exception:
            pass
        raise BusinessException(40001, "用户名或密码错误")

    if not admin.enabled:
        raise BusinessException(40301, "账号已禁用")

    admin_user_service.touch_login(db, admin)
    db.commit()
    db.refresh(admin)

    try:
        clear_admin_login_fail(req.username)
    except Exception:
        pass

    token, expires_at = create_admin_token(admin.id, admin.username)
    return ok({
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at.isoformat(),
        "password_changed": bool(admin.password_changed),
    })


@router.get("/me", summary="当前管理员信息")
def me(current: AdminUser = Depends(require_admin)):
    return ok(AdminUserRead.model_validate(current).model_dump(mode="json"))


@router.put("/me/password", summary="修改当前管理员密码")
def change_password(
    req: ChangePasswordRequest,
    current: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not verify_password(req.old_password, current.password_hash or ""):
        raise BusinessException(40001, "原密码错误")
    if req.old_password == req.new_password:
        raise BusinessException(40101, "新密码不能与旧密码相同")
    if len(req.new_password) < 8:
        raise BusinessException(40101, "新密码长度至少 8 位")

    admin_user_service.change_password(db, current, req.new_password)
    db.commit()
    return ok()
