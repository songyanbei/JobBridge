from app.services.pii_crypto_service import PiiCryptoError, PiiCryptoService, pii_digest


def test_encrypt_decrypt_binds_aad_and_key_version():
    service = PiiCryptoService({1: "old-secret", 2: "new-secret"}, active_key_version=2)
    sealed = service.encrypt("13800138000", field="phone", entity_type="user", entity_id="u1")
    assert sealed.key_version == 2
    assert sealed.digest == pii_digest("13800138000")
    assert service.decrypt(sealed.value, field="phone", entity_type="user", entity_id="u1") == "13800138000"

    try:
        service.decrypt(sealed.value, field="phone", entity_type="user", entity_id="u2")
    except PiiCryptoError:
        pass
    else:
        raise AssertionError("AAD substitution must fail closed")


def test_rotation_requires_previous_key_and_writes_active_version():
    old = PiiCryptoService({1: "old-secret"}, active_key_version=1)
    sealed = old.encrypt("wx_demo", field="wechat", entity_type="job", entity_id="7")
    rotated = PiiCryptoService({1: "old-secret", 2: "new-secret"}, active_key_version=2).rotate(
        sealed.value, field="wechat", entity_type="job", entity_id="7",
    )
    assert rotated.key_version == 2


def test_missing_key_and_corrupt_ciphertext_fail_closed():
    service = PiiCryptoService({1: "secret"}, active_key_version=1)
    sealed = service.encrypt("secret-value", field="phone", entity_type="user", entity_id="u1")
    with_missing_key = PiiCryptoService({}, active_key_version=1)
    for operation in (
        lambda: with_missing_key.decrypt(sealed.value, field="phone", entity_type="user", entity_id="u1"),
        lambda: service.decrypt(sealed.value[:-2] + b"xx", field="phone", entity_type="user", entity_id="u1"),
    ):
        try:
            operation()
        except PiiCryptoError:
            continue
        raise AssertionError("PII crypto failures must not use plaintext fallback")
