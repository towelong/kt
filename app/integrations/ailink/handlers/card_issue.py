"""开卡事件：CARD_ISSUE.SUCCESS / CARD_ISSUE.FAILED。"""

from __future__ import annotations

import logging

from app.integrations.ailink.dispatch import EventContext, EventHandler, dispatcher
from app.integrations.ailink.events import CardIssueEvent

logger = logging.getLogger(__name__)


@dispatcher.register
class CardIssueHandler(EventHandler[CardIssueEvent]):
    event_model = CardIssueEvent
    event_types = ("CARD_ISSUE.SUCCESS", "CARD_ISSUE.FAILED")

    async def process(self, event: CardIssueEvent, ctx: EventContext) -> None:
        data = event.data
        logger.info(
            "VCC 开卡事件 type=%s requestNo=%s applyStatus=%s fundStatus=%s"
            " 成功=%s 失败=%s 卡数=%d 首充=%s",
            event.event_type,
            data.request_no,
            data.apply_status,
            data.fund_status,
            data.success_count,
            data.fail_count,
            len(data.cards),
            data.initial_funding_status,
        )
        if data.fail_items:
            logger.warning(
                "VCC 开卡失败明细 event=%s 失败原因=%s",
                ctx.event_id,
                ",".join(item.fail_reason_code or "-" for item in data.fail_items),
            )
        # TODO: 在这里落库/通知业务方。
        # 注意：开卡 SUCCESS 不代表首充到账，首充等 CARD_OPERATION.RECHARGE_SUCCESS。
