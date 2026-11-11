"""AiLink VCC Webhook 接收端点：验签 → 解析 → 按 eventType 分发。

平台只认「HTTP 2xx 且正文是 ok」，所以成功路径必须返回纯文本 ok；
处理器抛异常就返回 500 让它按 1m/5m/15m/30m/1h/3h 重试，处理器自己按 eventId 幂等。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from app.config import Settings, get_settings
from app.integrations.ailink.dispatch import EventDispatcher
from app.integrations.ailink.errors import (
    AiLinkPayloadError,
    EventDispatchError,
    WebhookHeaderError,
    WebhookSignatureError,
)
from app.integrations.ailink.handlers import dispatcher
from app.integrations.ailink.webhooks import WEBHOOK_ACK, parse_event, verify_webhook_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def get_dispatcher() -> EventDispatcher:
    """默认用全局注册表；测试可通过 dependency_overrides 换成独立实例。"""
    return dispatcher


SettingsDep = Annotated[Settings, Depends(get_settings)]
DispatcherDep = Annotated[EventDispatcher, Depends(get_dispatcher)]


@router.post(
    "/ailink",
    response_class=PlainTextResponse,
    summary="AiLink VCC Webhook 接收",
    description="开卡、卡操作、卡交易、3DS、预算事件统一入口，按 X-VCC-WEBHOOK-EVENT 分发。",
)
async def receive_ailink_webhook(
    request: Request,
    settings: SettingsDep,
    event_dispatcher: DispatcherDep,
) -> PlainTextResponse:
    raw_body = await request.body()
    if len(raw_body) > settings.ailink_webhook_max_body_bytes:
        logger.warning("VCC Webhook 正文过大：%d 字节", len(raw_body))
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Webhook 正文过大")

    secret = (
        settings.ailink_webhook_secret.get_secret_value() if settings.ailink_webhook_secret else ""
    )
    if not secret:
        # 绝不跳过验签：没配 Secret 时直接拒绝，让配置问题暴露在日志里
        logger.error("收到 VCC Webhook 但未配置 AILINK_WEBHOOK_SECRET，拒绝处理")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook 未配置")

    try:
        verify_webhook_signature(raw_body, request.headers, secret)
    except WebhookHeaderError as exc:
        logger.warning("VCC Webhook 验签 Header 不合法：%s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "签名 Header 缺失或格式错误") from exc
    except WebhookSignatureError as exc:
        client_ip = request.client.host if request.client else "-"
        logger.warning("VCC Webhook 验签失败，来源 IP=%s", client_ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "签名校验失败") from exc

    try:
        event = parse_event(raw_body)
    except AiLinkPayloadError as exc:
        logger.warning("VCC Webhook 正文无法解析：%s", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请求正文不合法") from exc

    try:
        report = await event_dispatcher.dispatch(event)
    except EventDispatchError:
        # 平台会重试；处理器里不要吞异常，也不要在日志里打请求体
        logger.exception(
            "VCC Webhook 处理失败，等待重试：event=%s type=%s",
            event.event_id,
            event.event_type,
        )
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "处理失败") from None

    logger.info(
        "VCC Webhook 已处理 event=%s type=%s handlers=%s",
        report.event_id,
        report.event_type,
        ",".join(report.handler_names) or "-",
    )
    return PlainTextResponse(WEBHOOK_ACK)
