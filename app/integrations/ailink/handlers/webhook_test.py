"""测试事件：只在开发者配置里点“测试推送”时出现，不代表业务状态变化。"""

from __future__ import annotations

import logging

from app.integrations.ailink.dispatch import EventContext, EventHandler, dispatcher
from app.integrations.ailink.events import WebhookTestEvent

logger = logging.getLogger(__name__)


@dispatcher.register
class WebhookTestHandler(EventHandler[WebhookTestEvent]):
    event_model = WebhookTestEvent
    event_types = ("WEBHOOK_TEST",)

    async def process(self, event: WebhookTestEvent, ctx: EventContext) -> None:
        logger.info(
            "VCC Webhook 测试推送 event=%s message=%s",
            event.event_id,
            event.data.message,
        )
