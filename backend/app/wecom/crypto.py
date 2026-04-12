"""企业微信加解密与签名校验。

实现企微回调消息的签名校验和 AES 解密，符合企业微信官方加解密方案。
仅做加解密与签名，不包含业务判断。
"""
import base64
import hashlib
import struct

from Crypto.Cipher import AES


def verify_signature(token: str, timestamp: str, nonce: str, encrypt: str, msg_signature: str) -> bool:
    """校验回调签名。

    将 token、timestamp、nonce、encrypt 按字典序拼接后 SHA1 计算，
    与 msg_signature 对比。

    Args:
        token: 企微后台配置的 Token
        timestamp: 回调中的 timestamp 参数
        nonce: 回调中的 nonce 参数
        encrypt: 密文（从 XML 中提取的 Encrypt 字段）
        msg_signature: 回调中的 msg_signature 参数

    Returns:
        签名是否匹配。
    """
    parts = sorted([token, timestamp, nonce, encrypt])
    raw = "".join(parts).encode("utf-8")
    computed = hashlib.sha1(raw).hexdigest()
    return computed == msg_signature


def decrypt_message(aes_key_base64: str, encrypt: str, corp_id: str) -> str:
    """解密企微回调消息。

    使用 AES-256-CBC 解密，密钥由 EncodingAESKey Base64 解码得到，
    IV 为密钥前 16 字节。解密后去 PKCS#7 填充，
    提取 msg_len + msg_content，并校验 corp_id。

    Args:
        aes_key_base64: 企微后台配置的 EncodingAESKey（43 字符 Base64）
        encrypt: 密文字符串
        corp_id: 企业 ID，用于校验

    Returns:
        解密后的明文 XML。

    Raises:
        ValueError: 解密失败或 corp_id 校验不通过。
    """
    try:
        aes_key = base64.b64decode(aes_key_base64 + "=")
        iv = aes_key[:16]

        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        encrypted = base64.b64decode(encrypt)
        decrypted = cipher.decrypt(encrypted)

        # 去 PKCS#7 填充并校验
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32:
            raise ValueError(f"Invalid PKCS#7 padding value: {pad_len}")
        if not all(b == pad_len for b in decrypted[-pad_len:]):
            raise ValueError("Corrupted PKCS#7 padding")
        content = decrypted[:-pad_len]

        # 解析: 16 字节随机串 + 4 字节 msg_len (网络字节序) + msg + corp_id
        if len(content) < 20:
            raise ValueError(
                f"Decrypted content too short: {len(content)} bytes, need >= 20"
            )
        msg_len = struct.unpack("!I", content[16:20])[0]
        if 20 + msg_len > len(content):
            raise ValueError(
                f"Message length {msg_len} exceeds decrypted content bounds"
            )
        msg = content[20:20 + msg_len].decode("utf-8")
        from_corp_id = content[20 + msg_len:].decode("utf-8")

        if from_corp_id != corp_id:
            raise ValueError(
                f"Corp ID mismatch: expected '{corp_id}', got '{from_corp_id}'"
            )

        return msg

    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to decrypt WeCom message: {exc}") from exc


def encrypt_message(aes_key_base64: str, msg: str, corp_id: str, nonce: str | None = None) -> str:
    """加密回复消息（用于被动回复）。

    Args:
        aes_key_base64: EncodingAESKey
        msg: 明文消息
        corp_id: 企业 ID
        nonce: 随机 16 字节串（默认自动生成）

    Returns:
        Base64 编码的密文。
    """
    import os

    aes_key = base64.b64decode(aes_key_base64 + "=")
    iv = aes_key[:16]

    random_prefix = nonce.encode("utf-8")[:16] if nonce else os.urandom(16)
    msg_bytes = msg.encode("utf-8")
    corp_id_bytes = corp_id.encode("utf-8")
    msg_len = struct.pack("!I", len(msg_bytes))

    plaintext = random_prefix + msg_len + msg_bytes + corp_id_bytes

    # PKCS#7 填充到 AES block size (32 for 企微)
    block_size = 32
    pad_len = block_size - (len(plaintext) % block_size)
    plaintext += bytes([pad_len]) * pad_len

    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(plaintext)

    return base64.b64encode(encrypted).decode("utf-8")


def generate_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """生成签名（用于被动回复和主动推送签名）。"""
    parts = sorted([token, timestamp, nonce, encrypt])
    raw = "".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()
