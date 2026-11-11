"""3DS 验证码事件：只有显式订阅 CARD_3DS 的商户才会收到，验证码禁止落日志。"""

from __future__ import annotations

import logging

from app.integrations.ailink.dispatch import EventContext, EventHandler, dispatcher
from app.integrations.ailink.events import Card3dsEvent

logger = logging.getLogger(__name__)


@dispatcher.register
class Card3dsHandler(EventHandler[Card3dsEvent]):
    event_model = Card3dsEvent
    event_types = ("CARD_3DS.OTP_RECEIVED",)

    async def process(self, event: Card3dsEvent, ctx: EventContext) -> None:
        data = event.data
        # 只记 otpId/卡/商户，绝不记录 otpCode 明文
        logger.info(
            "VCC 3DS 验证码事件 otpId=%s card=%s 商户=%s 金额=%s %s",
            data.otp_id,
            data.card_id,
            data.merchant_name,
            data.original_amount,
            data.original_currency,
        )
        # TODO: 在这里交给需要验证码的业务流程，访问权限要收紧。
