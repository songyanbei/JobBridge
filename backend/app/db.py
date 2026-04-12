"""SQLAlchemy 数据库引擎与会话管理。

设计说明：
- 使用 SQLAlchemy 2.0 同步 API
- FastAPI 依赖注入通过 `get_db()` 获取 session
- `Base` 是所有 ORM 模型的声明基类
"""
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""
    pass


engine = create_engine(
    settings.db_url,
    pool_pre_ping=True,       # 自动探活，防止 MySQL 8 小时超时断连
    pool_recycle=3600,        # 1 小时回收连接
    pool_size=10,
    max_overflow=20,
    echo=settings.is_development,  # 开发环境打印 SQL
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：注入一个数据库会话，请求结束自动关闭。

    使用示例：
        @app.get("/items")
        def list_items(db: Session = Depends(get_db)):
            return db.query(Item).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
