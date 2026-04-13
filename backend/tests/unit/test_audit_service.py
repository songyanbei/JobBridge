"""audit_service 单元测试。"""
from unittest.mock import MagicMock

import pytest

from app.services.audit_service import (
    _aggregate_risk,
    _scan_sensitive_words,
    audit_content,
    audit_content_only,
    write_audit_log_for_result,
)


def _make_sensitive_word(word, level, enabled=1, category=None):
    sw = MagicMock()
    sw.word = word
    sw.level = level
    sw.enabled = enabled
    sw.category = category
    return sw


class TestScanSensitiveWords:
    def test_matches_high_word(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            _make_sensitive_word("传销", "high"),
        ]
        hits = _scan_sensitive_words("这个是传销骗局", db)
        assert len(hits) == 1
        assert hits[0]["level"] == "high"

    def test_no_match(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            _make_sensitive_word("传销", "high"),
        ]
        hits = _scan_sensitive_words("苏州电子厂招普工", db)
        assert len(hits) == 0

    def test_multiple_hits(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            _make_sensitive_word("传销", "high"),
            _make_sensitive_word("高薪", "low"),
        ]
        hits = _scan_sensitive_words("传销高薪招聘", db)
        assert len(hits) == 2


class TestAggregateRisk:
    def test_empty_passes(self):
        status, reason = _aggregate_risk([])
        assert status == "passed"

    def test_high_rejects(self):
        status, _ = _aggregate_risk([{"word": "传销", "level": "high"}])
        assert status == "rejected"

    def test_mid_pending(self):
        status, _ = _aggregate_risk([{"word": "刷单", "level": "mid"}])
        assert status == "pending"

    def test_low_passes_with_reason(self):
        status, reason = _aggregate_risk([{"word": "高薪", "level": "low"}])
        assert status == "passed"
        assert "高薪" in reason

    def test_high_overrides_mid(self):
        status, _ = _aggregate_risk([
            {"word": "刷单", "level": "mid"},
            {"word": "传销", "level": "high"},
        ])
        assert status == "rejected"


class TestAuditContent:
    def test_clean_content_passes(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        result = audit_content("苏州电子厂招普工", "job", 1, db)
        assert result.status == "passed"

    def test_high_risk_rejects_and_writes_log(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            _make_sensitive_word("传销", "high"),
        ]
        result = audit_content("这是传销", "job", 1, db)
        assert result.status == "rejected"
        # Should have written audit_log
        db.add.assert_called()

    def test_mid_risk_pending_no_audit_log(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            _make_sensitive_word("刷单", "mid"),
        ]
        result = audit_content("刷单日结", "job", 1, db)
        assert result.status == "pending"
        # pending should NOT write audit_log
        db.add.assert_not_called()
