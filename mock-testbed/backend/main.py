"""Mock 企业微信测试台 · FastAPI 入口。

运行：
    cd mock-testbed/backend
    source .venv/bin/activate
    uvicorn main:app --host 0.0.0.0 --port 8001 --reload

此服务**仅用于 Demo / 联调**，禁止挂到生产流量。
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from routes import router as mock_router

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mock-wework")

app = FastAPI(
    title="Mock 企业微信测试台",
    description=(
        "JobBridge 项目的企业微信模拟沙箱。\n\n"
        "⚠️ 仅限本地 / 演示环境，禁止用于生产。"
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(mock_router)


@app.get("/health")
def health() -> dict:
    return {"errcode": 0, "errmsg": "ok", "service": "mock-wework-testbed"}


@app.get("/")
def root() -> dict:
    """根路径给个友好的说明，防止误访问一脸懵。"""
    return {
        "errcode": 0,
        "errmsg": "ok",
        "service": "mock-wework-testbed",
        "endpoints": [
            "GET  /mock/wework/users",
            "GET  /mock/wework/oauth2/authorize",
            "GET  /mock/wework/code2userinfo",
            "POST /mock/wework/inbound",
            "GET  /mock/wework/sse?external_userid=...",
            "GET  /health",
            "GET  /docs",
        ],
        "frontend": "http://localhost:5174",
    }


@app.on_event("startup")
async def _warn_not_for_production() -> None:
    logger.warning(
        "⚠️  Mock WeCom testbed starting on port %s. "
        "DO NOT USE IN PRODUCTION. CORS origins: %s",
        settings.port,
        settings.cors_origin_list,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=True,
    )
