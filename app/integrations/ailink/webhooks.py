"""Webhook 验签与解析。

验签原文 = eventId + "\\n" + eventType + "\\n" + timestamp + "\\n" + nonce
         + "\\n" + SHA256_HEX(RAW_REQUEST_BODY)
签名格式 v1=<hex>，密钥是 Webhook Secret（不是 API Secret）。
"""

from __future__ import annotations

import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.integrations.ailink.errors import (
    AiLinkPayloadError,
    WebhookHeaderError,
    WebhookSignatureError,
)
from app.integrations.ailink.events import WebhookEvent, event_model_for
from app.integrations.ailink.signing import body_sha256, sign

WEBHOOK_ACK = "ok"

HEADER_EVENT_ID = "x-vcc-webhook-id"
HEADER_EVENT_TYPE = "x-vcc-webhook-event"
HEADER_TIMESTAMP = "x-vcc-webhook-timestamp"
HEADER_NONCE = "x-vcc-webhook-nonce"
HEADER_SIGNATURE = "x-vcc-webhook-signature"

_REQUIRED_HEADERS = (
    HEADER_EVENT_ID,
    HEADER_EVENT_TYPE,
    HEADER_TIMESTAMP,
    HEADER_NONCE,
    HEADER_SIGNATURE,
)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Starlette 的 Headers 本身不区分大小写，普通 dict 也能用。"""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def verify_webhook_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    webhook_secret: str,
    *,
    max_skew_seconds: float | None = None,
) -> None:
    """校验 Webhook 签名；失败抛 WebhookHeaderError（401）或 WebhookSignatureError（403）。

    max_skew_seconds 默认关闭：平台重试最长 3 小时，timestamp 是本次投递时间，
    但在时钟漂移/排队场景下做窗口校验容易误杀，需要时再显式打开。
    """
    values = {name: _header(headers, name) for name in _REQUIRED_HEADERS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise WebhookHeaderError(f"Webhook 缺少验签 Header：{', '.join(missing)}")

    event_id = values[HEADER_EVENT_ID]
    event_type = values[HEADER_EVENT_TYPE]
    timestamp = values[HEADER_TIMESTAMP]
    nonce = values[HEADER_NONCE]
    signature = values[HEADER_SIGNATURE]
    assert event_id and event_type and timestamp and nonce and signature  # 上面已检查

    scheme, _, digest = signature.partition("=")
    if scheme.strip().lower() != "v1" or not digest:
        raise WebhookHeaderError("Webhook 签名格式必须是 v1=<hex>")

    if max_skew_seconds is not None:
        try:
            sent_at = int(timestamp) / 1000
        except ValueError as exc:
            raise WebhookHeaderError("Webhook 时间戳不是毫秒数") from exc
        if abs(time.time() - sent_at) > max_skew_seconds:
            raise WebhookSignatureError("Webhook 时间戳超出允许的时钟偏移")

    base = "\n".join([event_id, event_type, timestamp, nonce, body_sha256(raw_body)])
    expected = sign(webhook_secret, base)
    if not hmac.compare_digest(expected, digest.strip().lower()):
        raise WebhookSignatureError("Webhook 签名不匹配")


def parse_event(raw_body: bytes) -> WebhookEvent:
    """按 eventType 解析成类型化事件；未知类型返回 UnknownEvent。"""
    try:
        payload: Any = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AiLinkPayloadError("Webhook 正文不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise AiLinkPayloadError("Webhook 正文必须是 JSON 对象")

    event_type = payload.get("eventType")
    if not isinstance(event_type, str) or not event_type:
        raise AiLinkPayloadError("Webhook 缺少 eventType")

    model = event_model_for(event_type)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        details = ", ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}:{err['type']}"
            for err in exc.errors()
        )
        raise AiLinkPayloadError(
            f"Webhook {event_type} 不符合 {model.__name__} 结构（{details}）"
        ) from exc


def parse_and_verify(
    raw_body: bytes,
    headers: Mapping[str, str],
    webhook_secret: str,
    *,
    max_skew_seconds: float | None = None,
) -> WebhookEvent:
    verify_webhook_signature(
        raw_body,
        headers,
        webhook_secret,
        max_skew_seconds=max_skew_seconds,
    )
    return parse_event(raw_body)
