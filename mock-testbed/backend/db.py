"""Mock 企业微信测试台 · DB 引擎。

独立 SessionLocal，共享主后端的 MySQL 实例。
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings


class Base(DeclarativeBase):
    """沙箱独立的 declarative base（不共享主后端 models.Base）。"""


engine = create_engine(
    settings.db_dsn,
    pool_pre_ping=True,
    pool_recycle=3600,
    future=True,
    # 强制 utf8mb4 —— PyMySQL 默认 latin1，会导致中文显示成
    # `å¼ å·¥` 之类的 UTF-8→Latin-1 错位（mojibake）。
    # 即使 DSN 已带 ?charset=utf8mb4，这里再 belt-and-suspenders 一道
    connect_args={"charset": "utf8mb4"},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    """FastAPI Depends 入口。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
