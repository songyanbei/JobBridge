"""Session patch and delivery envelope are both bound to one delivery."""
from __future__ import annotations

import pytest

from app.services.recommendation_delivery_service import (
    decrypt_body,
    encrypt_body,
    encrypt_session_patch,
)

pytestmark = pytest.mark.integration


def test_envelope_aad_prevents_cross_delivery_replay(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "recommendation_content_key", "integration-key")
    sealed = encrypt_body(
        "integration body",
        delivery_id="delivery-a",
        userid="viewer-a",
    )
    assert decrypt_body(
        sealed, delivery_id="delivery-a", userid="viewer-a",
    ) == "integration body"
    with pytest.raises(Exception):
        decrypt_body(sealed, delivery_id="delivery-b", userid="viewer-a")
    assert encrypt_session_patch(
        '{"version": 1}',
        delivery_id="delivery-a",
        userid="viewer-a",
    )
