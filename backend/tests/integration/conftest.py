"""Shared real-MySQL/Redis fixtures for recommendation acceptance tests."""
from __future__ import annotations

from uuid import uuid4

import pytest

import app.models  # noqa: F401 - register every table before create_all
from app.db import Base, SessionLocal, engine


@pytest.fixture(scope="session", autouse=True)
def recommendation_mysql_schema():
    """Keep the integration suite runnable both after migrations and standalone."""
    Base.metadata.create_all(bind=engine)


@pytest.fixture()
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def unique_prefix():
    return f"rec-it-{uuid4().hex[:16]}"
