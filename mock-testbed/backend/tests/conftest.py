"""pytest fixtures for mock-testbed backend.

策略：
- 默认所有测试用**进程内 sqlite :memory:** DB，避免依赖真实 MySQL
  （契约测试只关心字段形态、幂等、返回结构，不关心 MySQL 特性）
- Redis 测试用 fakeredis（已通过 redis-py 的可替换接口支持）
  —— 如果 fakeredis 不可用，相关 pubsub 测试会 skip
- client fixture 基于 FastAPI TestClient

运行：
    cd mock-testbed/backend
    source .venv/bin/activate   # 或 .venv/Scripts/activate
    pip install pytest pytest-asyncio fakeredis
    pytest -v
"""
import os
import sys
from pathlib import Path

import pytest

# 让 tests/ 能 import backend/ 根下的模块
_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

# 测试用 sqlite 覆盖 DSN（Settings 加载前设置 env）
os.environ.setdefault("MOCK_DB_DSN", "sqlite:///:memory:")
os.environ.setdefault("MOCK_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture(scope="session")
def _engine():
    """用 sqlite 建所有表。"""
    from sqlalchemy import create_engine
    from db import Base
    import models  # noqa: F401  确保模型注册到 Base.metadata

    engine = create_engine("sqlite:///:memory:", future=True, echo=False)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db(_engine, monkeypatch):
    """每个测试一个独立 SessionLocal，且绑定到 sqlite engine。"""
    from sqlalchemy.orm import sessionmaker
    import db as db_module

    TestSession = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(db_module, "engine", _engine)

    session = TestSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def seeded_users(db):
    """注入 4 个 wm_mock_* 用户。"""
    from models import MockUser

    rows = [
        MockUser(external_userid="wm_mock_worker_001", role="worker", display_name="张工"),
        MockUser(external_userid="wm_mock_worker_002", role="worker", display_name="李师傅"),
        MockUser(external_userid="wm_mock_factory_001", role="factory", display_name="华东电子厂"),
        MockUser(external_userid="wm_mock_broker_001", role="broker", display_name="速聘中介"),
    ]
    for u in rows:
        db.merge(u)
    db.commit()
    return rows


@pytest.fixture
def fakeredis_conn(monkeypatch):
    """用 fakeredis 替换 redis.Redis.from_url。"""
    try:
        import fakeredis
    except ImportError:
        pytest.skip("fakeredis not installed; install with: pip install fakeredis")

    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)

    import redis as real_redis

    def _fake_from_url(url, **kwargs):
        return fakeredis.FakeRedis(server=server, decode_responses=kwargs.get("decode_responses", False))

    monkeypatch.setattr(real_redis.Redis, "from_url", staticmethod(_fake_from_url))
    return fake


@pytest.fixture
def client(monkeypatch, db, fakeredis_conn):
    """FastAPI TestClient，依赖注入 get_db 走 sqlite session。"""
    from fastapi.testclient import TestClient
    import main as main_module
    from db import get_db

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    main_module.app.dependency_overrides[get_db] = _override_get_db
    with TestClient(main_module.app) as c:
        yield c
    main_module.app.dependency_overrides.clear()
