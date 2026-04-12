"""数据库集成测试（需要真实 MySQL）。"""
import pytest
from sqlalchemy import text, inspect

from app.db import Base, engine

pytestmark = pytest.mark.integration


class TestDatabaseConnection:
    """数据库连接与建表测试。"""

    def test_connection(self):
        """可以连接到数据库。"""
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.scalar() == 1

    def test_create_all_tables(self):
        """Base.metadata.create_all 可以成功建表。"""
        # 确保模型已注册到 metadata
        import app.models  # noqa: F401

        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        expected = {
            "user", "job", "resume", "conversation_log", "audit_log",
            "dict_city", "dict_job_category", "dict_sensitive_word",
            "system_config", "admin_user", "wecom_inbound_event",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_table_count(self):
        """至少 11 张业务表存在。"""
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert len(tables) >= 11
