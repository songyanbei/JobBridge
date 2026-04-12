"""FastAPI 应用入口。

运行方式：
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.db import engine

app = FastAPI(
    title="JobBridge 招聘撮合平台",
    description="企业微信 + LLM 的招聘撮合后端（v1）",
    version="0.1.0",
    docs_url="/docs" if settings.is_development else None,
    redoc_url=None,
)

# CORS：通过 CORS_ORIGINS 环境变量配置，开发环境默认放开全部
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
