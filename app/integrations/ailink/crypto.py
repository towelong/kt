"""Webhook 敏感字段解密：AES-256-GCM，密钥由 Webhook Secret 派生（不是 API Secret）。

文档口径：key = HMAC-SHA256(webhookSecret, "vcc-webhook-sensitive-v1")，
密文 = base64url(iv[12] + ciphertext + tag[16])，不带 = 填充。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.integrations.ailink.errors import AiLinkDecryptError

_KEY_DERIVATION_LABEL = b"vcc-webhook-sensitive-v1"
_IV_LENGTH = 12
_TAG_LENGTH = 16
SUPPORTED_ALG = "AES-256-GCM"
SUPPORTED_VERSION = "v1"


def derive_sensitive_key(webhook_secret: str) -> bytes:
    """由商户配置的 Webhook Secret 派生 32 字节 AES key。"""
    return hmac.new(
        webhook_secret.encode("utf-8"),
        _KEY_DERIVATION_LABEL,
        hashlib.sha256,
    ).digest()


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        raise AiLinkDecryptError("敏感字段密文不是合法的 base64url") from exc


def decrypt_sensitive(
    ciphertext: str,
    key: bytes,
    *,
    alg: str = SUPPORTED_ALG,
    version: str = SUPPORTED_VERSION,
) -> str:
    """解密单个敏感字段，返回明文；失败一律抛 AiLinkDecryptError。"""
    if alg != SUPPORTED_ALG or version != SUPPORTED_VERSION:
        raise AiLinkDecryptError(f"不支持的敏感字段加密算法：{alg}/{version}")

    raw = _b64url_decode(ciphertext)
    if len(raw) <= _IV_LENGTH + _TAG_LENGTH:
        raise AiLinkDecryptError("敏感字段密文长度不足")

    iv, sealed = raw[:_IV_LENGTH], raw[_IV_LENGTH:]
    try:
        plaintext = AESGCM(key).decrypt(iv, sealed, None)
    except InvalidTag as exc:
        raise AiLinkDecryptError("敏感字段解密失败：Webhook Secret 不匹配或密文被篡改") from exc
    except (ValueError, TypeError) as exc:
        raise AiLinkDecryptError("敏感字段解密失败：密文格式不合法") from exc

    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AiLinkDecryptError("敏感字段明文不是 UTF-8") from exc
