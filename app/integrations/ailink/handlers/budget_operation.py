"""预算事件：预算组创建、充值成功、转出成功。"""

from __future__ import annotations

import logging

from app.integrations.ailink.dispatch import EventContext, EventHandler, dispatcher
from app.integrations.ailink.events import BudgetOperationEvent

logger = logging.getLogger(__name__)


@dispatcher.register
class BudgetOperationHandler(EventHandler[BudgetOperationEvent]):
    event_model = BudgetOperationEvent
    categories = ("BUDGET_OPERATION",)

    async def process(self, event: BudgetOperationEvent, ctx: EventContext) -> None:
        data = event.data
        logger.info(
            "VCC 预算事件 type=%s budgetNo=%s 操作=%s 状态=%s 金额=%s %s",
            event.event_type,
            data.budget_no,
            data.operation_type,
            data.operation_status,
            data.amount,
            data.currency or "",
        )
        # TODO: 在这里落库/通知业务方。
