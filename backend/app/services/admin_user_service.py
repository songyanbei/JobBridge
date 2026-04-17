"""运营管理员账号 service（Phase 5）。

只做用户查询 / 密码字段更新；不直接颁发 token、不直接接触 Redis。
登录编排 + 失败计数在 `api/admin/auth.py` 路由层完成，保持 service 纯粹。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import AdminUser


def get_by_username(db: Session, username: str) -> AdminUser | None:
    return db.query(AdminUser).filter(AdminUser.username == username).first()


def get_by_id(db: Session, admin_id: int) -> AdminUser | None:
    return db.query(AdminUser).filter(AdminUser.id == admin_id).first()


def touch_login(db: Session, admin: AdminUser) -> None:
    """更新 last_login_at。调用方负责 commit。"""
    admin.last_login_at = datetime.now()


def change_password(db: Session, admin: AdminUser, new_plain: str) -> None:
    """更新密码并置 password_changed=1。调用方负责 commit。"""
    admin.password_hash = hash_password(new_plain)
    admin.password_changed = 1
