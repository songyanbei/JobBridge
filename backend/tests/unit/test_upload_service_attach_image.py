"""upload_service.attach_image 单元测试（Phase 4 新增）。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.schemas.conversation import SessionState
from app.services.upload_service import attach_image


def _make_record_mock(images=None):
    rec = MagicMock()
    rec.id = 100
    rec.images = list(images) if images else None
    return rec


class TestAttachImage:
    def test_empty_image_key_returns_error(self):
        session = SessionState(role="worker", current_intent="upload_resume")
        result = attach_image("u1", "", session, MagicMock())
        assert "保存失败" in result

    def test_no_record_found_returns_hint(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        session = SessionState(role="worker", current_intent="upload_resume")
        result = attach_image("u1", "key/img.jpg", session, db)
        assert "未找到正在处理" in result

    def test_duplicate_image_not_added(self):
        db = MagicMock()
        rec = _make_record_mock(images=["key/img.jpg"])
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = rec
        session = SessionState(role="worker", current_intent="upload_resume")
        result = attach_image("u1", "key/img.jpg", session, db)
        assert "已附加" in result
        assert rec.images == ["key/img.jpg"]

    def test_max_images_rejected(self):
        db = MagicMock()
        rec = _make_record_mock(images=[f"img_{i}.jpg" for i in range(5)])
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = rec
        session = SessionState(role="worker", current_intent="upload_resume")
        result = attach_image("u1", "new.jpg", session, db)
        assert "上限" in result

    def test_attaches_to_resume_for_worker(self):
        db = MagicMock()
        rec = _make_record_mock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = rec
        session = SessionState(role="worker", current_intent="upload_resume")
        result = attach_image("u1", "key/img.jpg", session, db)
        assert rec.images == ["key/img.jpg"]
        assert "简历" in result
        db.flush.assert_called_once()

    def test_attaches_to_job_for_factory_upload_intent(self):
        db = MagicMock()
        rec = _make_record_mock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = rec
        session = SessionState(role="factory", current_intent="upload_job")
        result = attach_image("u1", "key/img.jpg", session, db)
        assert rec.images == ["key/img.jpg"]
        assert "岗位" in result
