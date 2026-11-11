"""开卡事件：CARD_ISSUE.SUCCESS / CARD_ISSUE.FAILED。"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.integrations.ailink.dispatch import EventContext, EventHandler, dispatcher
from app.integrations.ailink.events import CardIssueEvent
from app.services.card_events import handle_card_issue

logger = logging.getLogger(__name__)


@dispatcher.register
class CardIssueHandler(EventHandler[CardIssueEvent]):
    event_model = CardIssueEvent
    event_types = ("CARD_ISSUE.SUCCESS", "CARD_ISSUE.FAILED")

    async def process(self, event: CardIssueEvent, ctx: EventContext) -> None:
        if ctx.session_scope is None:
            raise RuntimeError("分发器未注入 session_scope，开卡事件无法落库")
        settings = get_settings()
        secret = (
            settings.ailink_webhook_secret.get_secret_value()
            if settings.ailink_webhook_secret
            else None
        )
        with ctx.session_scope() as session:
            handle_card_issue(session, event, webhook_secret=secret)
