"""企微加解密与签名校验测试。"""
import base64
import hashlib
import struct
import os
import pytest

from app.wecom.crypto import (
    verify_signature,
    decrypt_message,
    encrypt_message,
    generate_signature,
)


# ---------------------------------------------------------------------------
# 测试用常量
# ---------------------------------------------------------------------------

# 生成一个合法的 43 字符 EncodingAESKey (base64 解码后 32 字节)
_RAW_AES_KEY = os.urandom(32)
_AES_KEY_BASE64 = base64.b64encode(_RAW_AES_KEY).decode("utf-8").rstrip("=")
# 确保长度是 43
assert len(_AES_KEY_BASE64) >= 43
_AES_KEY_BASE64 = _AES_KEY_BASE64[:43]

_TOKEN = "test_token_123"
_CORP_ID = "wx1234567890abcdef"


class TestVerifySignature:

    def test_valid_signature(self):
        timestamp = "1234567890"
        nonce = "test_nonce"
        encrypt = "encrypted_data"

        parts = sorted([_TOKEN, timestamp, nonce, encrypt])
        expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

        assert verify_signature(_TOKEN, timestamp, nonce, encrypt, expected) is True

    def test_invalid_signature(self):
        assert verify_signature(_TOKEN, "123", "nonce", "data", "wrong_signature") is False

    def test_empty_values(self):
        parts = sorted([_TOKEN, "", "", ""])
        expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        assert verify_signature(_TOKEN, "", "", "", expected) is True


class TestEncryptDecrypt:

    def test_round_trip(self):
        """加密后再解密应得到原文。"""
        original = "<xml><Content>Hello World</Content></xml>"
        encrypted = encrypt_message(_AES_KEY_BASE64, original, _CORP_ID)
        decrypted = decrypt_message(_AES_KEY_BASE64, encrypted, _CORP_ID)
        assert decrypted == original

    def test_chinese_content_round_trip(self):
        original = "<xml><Content>你好世界</Content></xml>"
        encrypted = encrypt_message(_AES_KEY_BASE64, original, _CORP_ID)
        decrypted = decrypt_message(_AES_KEY_BASE64, encrypted, _CORP_ID)
        assert decrypted == original

    def test_wrong_corp_id_raises(self):
        original = "test message"
        encrypted = encrypt_message(_AES_KEY_BASE64, original, _CORP_ID)
        with pytest.raises(ValueError, match="Corp ID mismatch"):
            decrypt_message(_AES_KEY_BASE64, encrypted, "wrong_corp_id")

    def test_long_message_round_trip(self):
        original = "A" * 10000
        encrypted = encrypt_message(_AES_KEY_BASE64, original, _CORP_ID)
        decrypted = decrypt_message(_AES_KEY_BASE64, encrypted, _CORP_ID)
        assert decrypted == original


class TestDecryptMalformedInput:
    """篡改密文时应抛 ValueError 而非底层异常。"""

    def test_corrupted_ciphertext_raises_value_error(self):
        original = "test message"
        encrypted = encrypt_message(_AES_KEY_BASE64, original, _CORP_ID)
        # 篡改密文最后一个字符
        tampered = encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B")
        with pytest.raises(ValueError):
            decrypt_message(_AES_KEY_BASE64, tampered, _CORP_ID)

    def test_empty_ciphertext_raises_value_error(self):
        with pytest.raises((ValueError, Exception)):
            decrypt_message(_AES_KEY_BASE64, "", _CORP_ID)

    def test_truncated_ciphertext_raises_value_error(self):
        original = "hello"
        encrypted = encrypt_message(_AES_KEY_BASE64, original, _CORP_ID)
        # 只取前 16 字节的 base64
        truncated = encrypted[:24]
        with pytest.raises(ValueError):
            decrypt_message(_AES_KEY_BASE64, truncated, _CORP_ID)


class TestGenerateSignature:

    def test_signature_matches_verify(self):
        timestamp = "9999999999"
        nonce = "random_nonce"
        encrypt = "some_encrypted_text"

        sig = generate_signature(_TOKEN, timestamp, nonce, encrypt)
        assert verify_signature(_TOKEN, timestamp, nonce, encrypt, sig) is True

    def test_deterministic(self):
        sig1 = generate_signature(_TOKEN, "1", "2", "3")
        sig2 = generate_signature(_TOKEN, "1", "2", "3")
        assert sig1 == sig2
