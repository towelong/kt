"""任务 payload 的落库加密（AES-256-GCM）。

订阅任务里的 session、卡信息是敏感数据：只以密文落 `task.payload_ciphertext`，
提交给 BYOC 后立即清空。密钥来自 `TASK_PAYLOAD_KEY`，32 字节 base64 或任意字符串
（后者内部做 SHA256 派生）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_LENGTH = 12


def derive_payload_key(secret: str) -> bytes:
    try:
        raw = base64.b64decode(secret, validate=True)
    except (ValueError, TypeError):
        raw = b""
    if len(raw) == 32:
        return raw
    return hashlib.sha256(secret.encode("utf-8")).digest()


def encrypt_payload(payload: dict, secret: str) -> str:
    key = derive_payload_key(secret)
    iv = os.urandom(_IV_LENGTH)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sealed = AESGCM(key).encrypt(iv, data, None)
    return base64.urlsafe_b64encode(iv + sealed).decode().rstrip("=")


def decrypt_payload(token: str, secret: str) -> dict:
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        raise ValueError("任务 payload 密文不是合法 base64url") from exc
    if len(raw) <= _IV_LENGTH + 16:
        raise ValueError("任务 payload 密文长度不足")
    key = derive_payload_key(secret)
    try:
        data = AESGCM(key).decrypt(raw[:_IV_LENGTH], raw[_IV_LENGTH:], None)
    except InvalidTag as exc:
        raise ValueError("任务 payload 解密失败：密钥不匹配或密文被篡改") from exc
    decoded = json.loads(data)
    if not isinstance(decoded, dict):
        raise ValueError("任务 payload 不是 JSON 对象")
    return decoded
