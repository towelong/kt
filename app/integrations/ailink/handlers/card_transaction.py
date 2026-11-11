"""卡交易事件：PROCESSING / AUTH_* / SETTLED / REFUND / REVERSE / CORRECTION。"""

from __future__ import annotations

import logging

from app.integrations.ailink.dispatch import EventContext, EventHandler, dispatcher
from app.integrations.ailink.events import CardTransactionEvent

logger = logging.getLogger(__name__)


@dispatcher.register
class CardTransactionHandler(EventHandler[CardTransactionEvent]):
    event_model = CardTransactionEvent
    event_types = (
        "CARD_TRANSACTION.PROCESSING",
        "CARD_TRANSACTION.AUTH_SUCCESS",
        "CARD_TRANSACTION.AUTH_FAILED",
        "CARD_TRANSACTION.SETTLED",
        "CARD_TRANSACTION.REFUND_SUCCESS",
        "CARD_TRANSACTION.REVERSE_SUCCESS",
        "CARD_TRANSACTION.CORRECTION",
    )

    async def process(self, event: CardTransactionEvent, ctx: EventContext) -> None:
        data = event.data
        logger.info(
            "VCC 卡交易事件 type=%s transactionId=%s related=%s status=%s"
            " 类型=%s 实际金额=%s %s 卡=%s 商户=%s",
            event.event_type,
            data.transaction_id,
            data.related_transaction_id or "-",
            data.event_status,
            data.transaction_type,
            data.actual_amount,
            data.actual_currency,
            data.card_id,
            data.merchant_name,
        )
        # TODO: 在这里推进本地订单状态机，按 transactionId 幂等。
        # AUTH_SUCCESS 和 SETTLED 是同一笔交易的两个阶段，不能当两笔消费入账；
        # PROCESSING 不作为入账依据，等成功/失败/清算/退款/撤销事件。
