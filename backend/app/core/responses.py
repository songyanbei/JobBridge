"""统一响应构造器（Phase 5）。

配合 `main.py` 全局异常处理器，所有 `/admin/*` / `/api/events/*`
接口统一使用 `{code, message, data}` 结构：

    success:  {"code": 0, "message": "ok", "data": {...}}
    paged:    {"code": 0, "message": "ok",
               "data": {"items": [...], "total": N, "page": p, "size": s, "pages": P}}
    error:    {"code": <int>, "message": "...", "data": null | {...}}
"""
from __future__ import annotations

from typing import Any


def ok(data: Any = None) -> dict:
    """成功响应。"""
    return {"code": 0, "message": "ok", "data": data}


def fail(code: int, message: str, data: Any = None) -> dict:
    """失败响应。"""
    return {"code": code, "message": message, "data": data}


def paged(items: list, total: int, page: int, size: int) -> dict:
    """分页响应，data 结构与 `core.pagination.PageResult` 保持一致。"""
    pages = (total + size - 1) // size if size > 0 else 0
    return ok({
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": pages,
    })
