"""请求签名：签名原文与文档的在线测试脚本保持一致。

原文 = HTTP_METHOD + "\\n" + REQUEST_PATH + "\\n" + TIMESTAMP + "\\n" + NONCE
     + "\\n" + SHA256_HEX(RAW_REQUEST_BODY)
签名 = HMAC-SHA256(API Secret, 原文) 的小写 hex（不带 v1= 前缀）。
"""

from __future__ import annotations

import hashlib
import hmac


def body_sha256(body: bytes) -> str:
    """对准备发送的原始字节做 SHA256；必须是真正发出去的那串字节。"""
    return hashlib.sha256(body).hexdigest()


def signature_base(
    *,
    method: str,
    path: str,
    timestamp_ms: str,
    nonce: str,
    body: bytes,
) -> str:
    return "\n".join([method.upper(), path, timestamp_ms, nonce, body_sha256(body)])


def sign(secret: str, message: str) -> str:
    """HMAC-SHA256 小写 hex，请求签名和 Webhook 验签共用。"""
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def request_signature(
    secret: str,
    *,
    method: str,
    path: str,
    timestamp_ms: str,
    nonce: str,
    body: bytes,
) -> str:
    base = signature_base(
        method=method,
        path=path,
        timestamp_ms=timestamp_ms,
        nonce=nonce,
        body=body,
    )
    return sign(secret, base)
