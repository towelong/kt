"""卡操作事件：充值、转出、冻结、解冻、销卡、限额/备注变更、风控销卡。"""

from __future__ import annotations

import logging

from app.integrations.ailink.dispatch import EventContext, EventHandler, dispatcher
from app.integrations.ailink.events import CardOperationEvent
from app.services.card_events import handle_card_operation

logger = logging.getLogger(__name__)


@dispatcher.register
class CardOperationHandler(EventHandler[CardOperationEvent]):
    event_model = CardOperationEvent
    # 按大类兜底：CARD_OPERATION.* 新增事件（如 RECHARGE_FAILED）不用改这里
    categories = ("CARD_OPERATION", "CARD")

    async def process(self, event: CardOperationEvent, ctx: EventContext) -> None:
        if ctx.session_scope is None:
            raise RuntimeError("分发器未注入 session_scope，卡操作事件无法落库")
        with ctx.session_scope() as session:
            handle_card_operation(session, event)

        data = event.data
        logger.info(
            "VCC 卡操作事件 type=%s requestNo=%s 操作=%s 状态=%s 卡=%s 金额=%s %s 退款=%s %s",
            event.event_type,
            data.request_no,
            data.operation_type,
            data.operation_status,
            data.card_id,
            data.amount,
            data.currency or "",
            data.refund_amount,
            data.refund_currency or "",
        )
        if event.event_type == "CARD.RISK_CANCELLED":
            logger.warning(
                "VCC 风控销卡 event=%s card=%s 原因=%s 触发交易=%s",
                ctx.event_id,
                data.card_id,
                data.reason,
                data.source_transaction_id,
            )
        # TODO: 在这里落库/通知业务方。
        # 销卡 refundAmount=0.00 只表示本条没带退款，之后还可能收到 WITHDRAW_SUCCESS。
