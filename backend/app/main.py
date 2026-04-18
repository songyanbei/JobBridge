"""FastAPI 应用入口。

运行方式：
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Phase 5：注册 /admin/* 与 /api/events/* 路由，并挂载统一响应 / 全局异常处理。
Phase 7：在 lifespan 内启动 APScheduler（同进程内嵌，非独立容器）。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.events import router as events_router
from app.api.webhook import router as webhook_router
from app.config import settings
from app.core.exceptions import AppError, BusinessException
from app.core.responses import fail
from app.db import engine
from app.tasks import scheduler as task_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启停阶段挂载 APScheduler（Phase 7 §3.1 模块 A）。

    - 启动阶段：`task_scheduler.start()` 构造 BackgroundScheduler 并注册任务。
    - 关闭阶段：`shutdown(wait=False)` 让进程能快速退出，分布式锁靠 TTL 兜底。
    """
    task_scheduler.start()
    try:
        yield
    finally:
        task_scheduler.shutdown()

OPENAPI_TAGS = [
    {"name": "system", "description": "系统健康检查与元数据。"},
    {"name": "admin-auth", "description": "运营后台鉴权：登录、当前用户、修改密码。"},
    {"name": "admin-audit", "description": "审核工作台：队列、详情、软锁、pass/reject/edit/undo。"},
    {"name": "admin-accounts", "description": "账号管理：厂家 / 中介 / 工人 / 黑名单 + Excel 批量导入。"},
    {"name": "admin-jobs", "description": "岗位管理：列表 / 编辑 / 下架 / 延期 / 取消下架 / CSV 导出。"},
    {"name": "admin-resumes", "description": "简历管理：列表 / 编辑 / 下架 / 延期 / CSV 导出。"},
    {"name": "admin-dicts", "description": "字典管理：城市 / 工种 / 敏感词（含批量导入）。"},
    {"name": "admin-config", "description": "系统配置读取与单项更新；危险项变更会写 audit_log。"},
    {"name": "admin-reports", "description": "数据看板：dashboard / trends / top / funnel / export。"},
    {"name": "admin-logs", "description": "对话日志查询与导出（必须带 userid + 时间范围）。"},
    {"name": "events", "description": "外部事件回传（小程序点击等），走 X-Event-Api-Key 鉴权。"},
    {"name": "wecom-webhook", "description": "企业微信回调入口（内部使用，非后台用户直连）。"},
]


app = FastAPI(
    title="JobBridge 招聘撮合平台 API",
    description=(
        "企业微信 + LLM 的招聘撮合后端 API（v1）。\n\n"
        "**统一响应协议**：所有 `/admin/*` 与 `/api/events/*` 接口均返回 HTTP 200，\n"
        "以 `{\"code\": 0 | <err>, \"message\": \"...\", \"data\": ...}` 结构区分成败。\n\n"
        "**鉴权**：\n"
        "- `/admin/*`：Bearer Token（`POST /admin/login` 获取），请求头 `Authorization: Bearer <token>`\n"
        "- `/api/events/*`：请求头 `X-Event-Api-Key: <EVENT_API_KEY>`（与 JWT 分离）\n\n"
        "**错误码范围**：\n"
        "- `40001`–`40099` 鉴权（40001 账密错误 / 40002 token 过期 / 40003 token 无效）\n"
        "- `40101` 参数错误\n"
        "- `40301` 权限不足\n"
        "- `40401` 资源不存在\n"
        "- `40901` 软锁冲突 / `40902` 乐观锁冲突 / `40903` 撤销窗口已过 / `40904` 业务冲突\n"
        "- `50001` 内部错误 / `50101` LLM 异常\n\n"
        "详见 `docs/frontend-handoff.md`。"
    ),
    version="0.1.0",
    docs_url="/docs" if settings.is_development else None,
    redoc_url="/redoc" if settings.is_development else None,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

# CORS：通过 CORS_ORIGINS 环境变量配置，开发环境默认放开全部
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 全局异常处理（Phase 5 §3.3）
# ---------------------------------------------------------------------------

@app.exception_handler(BusinessException)
async def business_exception_handler(request: Request, exc: BusinessException):
    """业务异常 → 200 + 统一错误码响应体。

    业务异常本身不是 HTTP 错误，所以统一以 HTTP 200 返回，由 code 字段区分成败。
    """
    return JSONResponse(status_code=200, content=fail(exc.code, exc.message, exc.data))


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """兼容 Phase 3 的字符串-code 异常：统一降级为 50001。"""
    return JSONResponse(
        status_code=200,
        content=fail(50001, exc.message or str(exc.code), {"legacy_code": exc.code}),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Pydantic 参数错误 → 40101。"""
    errors = exc.errors()
    # 简化 error 字段，避免把内部对象序列化出错
    simplified = [
        {"loc": list(err.get("loc", [])), "msg": err.get("msg"), "type": err.get("type")}
        for err in errors
    ]
    return JSONResponse(
        status_code=200,
        content=fail(40101, "参数错误", {"fields": simplified}),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """FastAPI 原生 HTTPException → 映射到 4xxxx / 5xxxx。

    约定：HTTP 401 → 40001 / HTTP 403 → 40301 / HTTP 404 → 40401 /
         HTTP 422 → 40101 / 其它 5xx → 50001
    """
    mapping = {
        400: (40101, "参数错误"),
        401: (40001, "未授权"),
        403: (40301, "权限不足"),
        404: (40401, "资源不存在"),
        405: (40101, "方法不允许"),
        409: (40900, "资源冲突"),
        422: (40101, "参数错误"),
    }
    code, default_msg = mapping.get(exc.status_code, (50001, "内部错误"))
    detail = exc.detail if isinstance(exc.detail, str) else default_msg
    # Webhook 路由依赖 403 的原生行为（企微校验失败），不做重写
    if request.url.path.startswith("/webhook/"):
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})
    return JSONResponse(status_code=200, content=fail(code, detail or default_msg))


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------

# 企微回调路由（Phase 4）
app.include_router(webhook_router)
# 运营后台 API（Phase 5）
app.include_router(admin_router)
# 小程序事件回传（Phase 5）
app.include_router(events_router)


@app.get("/health", tags=["system"])
def health_check():
    """健康检查，检测应用与数据库状态。"""
    db_ok = False
    db_error: str | None = None
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    return {
        "status": "ok" if db_ok else "degraded",
        "env": settings.app_env,
        "version": app.version,
        "db": {
            "ok": db_ok,
            "error": db_error,
        },
    }
