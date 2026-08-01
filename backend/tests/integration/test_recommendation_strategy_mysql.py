"""MySQL DDL/JSON/index contracts for strategy control-plane tables."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from app.db import engine

pytestmark = pytest.mark.integration


def test_mysql_has_strategy_control_plane_tables_and_constraints():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected = {
        "recommendation_strategy_version",
        "recommendation_strategy_release",
        "recommendation_release_history",
        "recommendation_runtime_control",
    }
    assert expected <= tables
    indexes = {
        index["name"]
        for index in inspector.get_indexes("recommendation_strategy_version")
    }
    assert "idx_recommendation_version_status" in indexes


def test_mysql_supports_json_strategy_parameters_and_runtime_revision():
    with engine.connect() as conn:
        column = conn.execute(text(
            "SELECT DATA_TYPE FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name='recommendation_strategy_version' "
            "AND column_name='parameters'"
        )).scalar_one()
        revision = conn.execute(text(
            "SELECT revision FROM recommendation_runtime_control "
            "WHERE scope='global'"
        )).scalar_one_or_none()
    assert str(column).lower() == "json"
    assert revision is None or int(revision) >= 1
