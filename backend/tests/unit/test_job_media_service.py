import pytest
from app.config import settings
from app.services.storage_reference_service import normalize_storage_reference


def test_normalizes_raw_key_and_local_url():
    assert normalize_storage_reference("images/user/a.jpg") == "images/user/a.jpg"
    assert normalize_storage_reference("/files/images/user/a.jpg") == "images/user/a.jpg"


def test_normalizes_trusted_signed_url(monkeypatch):
    monkeypatch.setattr(settings, "oss_trusted_origins", "https://assets.example.com")
    assert normalize_storage_reference(
        "https://assets.example.com/files/images/a.jpg?signature=secret#preview"
    ) == "images/a.jpg"


@pytest.mark.parametrize("value", [
    "../secret.jpg",
    "images/../secret.jpg",
    "C:/secret.jpg",
    "images\\secret.jpg",
    "images/%ZZ.jpg",
    "images/%252e%252e/secret.jpg",
    "https://evil.example/files/images/a.jpg?signature=secret",
    "/other/images/a.jpg",
])
def test_rejects_unsafe_or_untrusted_references(value):
    with pytest.raises(ValueError):
        normalize_storage_reference(value)
