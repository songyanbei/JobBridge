"""共享 test fixtures。"""
import os
import sys

import pytest

# 确保 backend/app 可被 import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# 标记：需要真实 MySQL / Redis 的集成测试
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires real MySQL / Redis")


def pytest_collection_modifyitems(config, items):
    """未设置 RUN_INTEGRATION 环境变量时自动跳过集成测试。"""
    if os.environ.get("RUN_INTEGRATION"):
        return
    skip_integration = pytest.mark.skip(reason="需设置 RUN_INTEGRATION=1 并启动 MySQL/Redis")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
