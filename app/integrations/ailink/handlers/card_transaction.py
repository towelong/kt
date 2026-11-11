"""卡交易事件：PROCESSING / AUTH_* / SETTLED / REFUND / REVERSE / CORRECTION。"""

from __future__ import annotations

import logging

from app.integrations.ailink.dispatch import EventContext, EventHandler, dispatcher
from app.integrations.ailink.events import CardTransactionEvent
from app.services.card_events import handle_card_transaction

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
        if ctx.session_scope is None:
            raise RuntimeError("分发器未注入 session_scope，交易事件无法落库")
        with ctx.session_scope() as session:
            handle_card_transaction(session, event)
