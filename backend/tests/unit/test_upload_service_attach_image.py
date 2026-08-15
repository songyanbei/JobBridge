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
    rec.version = 1
    return rec


def _set_query_record(db, record):
    (
        db.query.return_value
        .filter.return_value
        .with_for_update.return_value
        .first.return_value
    ) = record


class TestAttachImage:
    def test_empty_image_key_returns_error(self):
        session = SessionState(
            role="worker", attachment_target_type="resume", attachment_target_id=100,
        )
        result = attach_image("u1", "", session, MagicMock())
        assert "保存失败" in result

    def test_no_record_found_returns_hint(self):
        db = MagicMock()
        _set_query_record(db, None)
        session = SessionState(
            role="worker", attachment_target_type="resume", attachment_target_id=100,
        )
        result = attach_image("u1", "key/img.jpg", session, db)
        assert "未找到正在处理" in result

    def test_duplicate_image_not_added(self):
        db = MagicMock()
        rec = _make_record_mock(images=["key/img.jpg"])
        _set_query_record(db, rec)
        session = SessionState(
            role="worker", attachment_target_type="resume", attachment_target_id=100,
        )
        result = attach_image("u1", "key/img.jpg", session, db)
        assert "已附加" in result
        assert rec.images == ["key/img.jpg"]

    def test_max_images_rejected(self):
        db = MagicMock()
        rec = _make_record_mock(images=[f"img_{i}.jpg" for i in range(5)])
        _set_query_record(db, rec)
        session = SessionState(
            role="worker", attachment_target_type="resume", attachment_target_id=100,
        )
        result = attach_image("u1", "new.jpg", session, db)
        assert "上限" in result

    def test_attaches_to_resume_for_worker(self):
        db = MagicMock()
        rec = _make_record_mock()
        _set_query_record(db, rec)
        session = SessionState(
            role="worker", attachment_target_type="resume", attachment_target_id=100,
        )
        result = attach_image("u1", "key/img.jpg", session, db)
        assert rec.images == ["key/img.jpg"]
        assert rec.version == 2
        assert "简历" in result
        db.flush.assert_called_once()
        filters = db.query.return_value.filter.call_args.args
        assert any(
            getattr(getattr(clause, "left", None), "key", None) == "audit_status"
            for clause in filters
        )

    def test_attaches_to_job_for_factory_upload_intent(self):
        db = MagicMock()
        rec = _make_record_mock()
        _set_query_record(db, rec)
        session = SessionState(
            role="factory", attachment_target_type="job", attachment_target_id=100,
        )
        result = attach_image("u1", "key/img.jpg", session, db)
        assert rec.images == ["key/img.jpg"]
        assert rec.version == 2
        assert "岗位" in result

    def test_current_intent_without_exact_target_never_queries_old_record(
        self, monkeypatch,
    ):
        db = MagicMock()
        session = SessionState(role="factory", current_intent="upload_job")
        discard_pending_media = MagicMock(return_value=True)
        monkeypatch.setattr(
            "app.services.job_media_service.discard_pending_media",
            discard_pending_media,
        )

        result = attach_image(
            "u1", "key/img.jpg", session, db, media_lifecycle_id=123,
        )

        assert "未找到正在处理" in result
        discard_pending_media.assert_called_once_with(db, 123)
