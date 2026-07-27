"""Recommendation envelopes never store plaintext body bytes."""
from __future__ import annotations

import pytest

from app.services.recommendation_delivery_service import encrypt_body

pytestmark = pytest.mark.integration


def test_ciphertext_does_not_contain_phone_number(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "recommendation_content_key", "integration-key")
    plaintext = "请联系 13800000000"
    ciphertext = encrypt_body(
        plaintext, delivery_id="redaction-integration", userid="viewer",
    )
    assert plaintext.encode("utf-8") not in ciphertext
    assert b"13800000000" not in ciphertext
