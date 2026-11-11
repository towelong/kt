"""分发器：精确/大类/兜底匹配、幂等、失败聚合。"""

import pytest

from app.integrations.ailink.dispatch import EventDispatcher, EventHandler
from app.integrations.ailink.errors import EventDispatchError
from app.integrations.ailink.events import CardTransactionEvent, WebhookEvent
from app.integrations.ailink.webhooks import parse_event


def make_event(make_body, event_type="CARD_ISSUE.SUCCESS", event_id="evt_1"):
    category = event_type.split(".", 1)[0]
    body = make_body(
        {
            "eventId": event_id,
            "eventType": event_type,
            "eventCategory": category,
            "eventTime": "2026-07-02 12:00:00",
            "apiVersion": "2026-06-01",
            "merchantId": 10001,
            "data": {},
        }
    )
    return parse_event(body)


def make_handler(calls, label, *, event_types=(), categories=(), model=WebhookEvent, error=None):
    class Handler(EventHandler[WebhookEvent]):
        event_model = model

        async def process(self, event: WebhookEvent, ctx) -> None:
            if error is not None:
                raise error
            calls.append((label, ctx.event_id))

    Handler.event_types = event_types
    Handler.categories = categories
    return Handler


async def test_exact_and_category_handlers_both_run(make_body):
    calls: list[tuple[str, str]] = []
    dispatcher = EventDispatcher()
    dispatcher.register(make_handler(calls, "exact", event_types=("CARD_ISSUE.SUCCESS",)))
    dispatcher.register(make_handler(calls, "category", categories=("CARD_ISSUE",)))

    report = await dispatcher.dispatch(make_event(make_body))

    assert sorted(label for label, _ in calls) == ["category", "exact"]
    assert len(report.handler_names) == 2


async def test_fallback_handler_used_when_nothing_matches(make_body):
    calls: list[tuple[str, str]] = []
    dispatcher = EventDispatcher()
    fallback = make_handler(calls, "fallback")
    dispatcher.register(fallback)

    await dispatcher.dispatch(make_event(make_body, event_type="SOMETHING.NEW"))

    assert calls == [("fallback", "evt_1")]


async def test_duplicate_event_id_only_runs_once(make_body):
    calls: list[tuple[str, str]] = []
    dispatcher = EventDispatcher()
    dispatcher.register(make_handler(calls, "exact", event_types=("CARD_ISSUE.SUCCESS",)))

    first = make_event(make_body)
    await dispatcher.dispatch(first)
    await dispatcher.dispatch(first)
    await dispatcher.dispatch(make_event(make_body, event_type="CARD_ISSUE.FAILED"))

    assert calls == [("exact", "evt_1")]


async def test_failure_is_aggregated_and_other_handlers_still_run(make_body):
    calls: list[tuple[str, str]] = []
    dispatcher = EventDispatcher()
    dispatcher.register(
        make_handler(
            calls,
            "boom",
            event_types=("CARD_ISSUE.SUCCESS",),
            error=RuntimeError("db down"),
        )
    )
    dispatcher.register(make_handler(calls, "ok", categories=("CARD_ISSUE",)))

    with pytest.raises(EventDispatchError) as info:
        await dispatcher.dispatch(make_event(make_body))

    assert calls == [("ok", "evt_1")]
    assert len(info.value.failures) == 1
    assert info.value.event_id == "evt_1"
    assert isinstance(info.value.failures[0], RuntimeError)


async def test_handler_with_wrong_event_model_fails_loudly(make_body):
    calls: list[tuple[str, str]] = []
    dispatcher = EventDispatcher()
    dispatcher.register(
        make_handler(
            calls,
            "typo",
            event_types=("CARD_ISSUE.SUCCESS",),
            model=CardTransactionEvent,
        )
    )

    with pytest.raises(EventDispatchError) as info:
        await dispatcher.dispatch(make_event(make_body))

    assert isinstance(info.value.failures[0], TypeError)
    assert calls == []


async def test_no_handler_returns_empty_report(make_body):
    dispatcher = EventDispatcher()
    report = await dispatcher.dispatch(make_event(make_body))
    assert report.handler_names == ()


async def test_context_is_timezone_aware(make_body):
    seen = {}

    class Probe(EventHandler[WebhookEvent]):
        event_model = WebhookEvent
        event_types = ("CARD_ISSUE.SUCCESS",)

        async def process(self, event, ctx) -> None:
            seen["received_at"] = ctx.received_at
            seen["merchant_id"] = ctx.merchant_id

    dispatcher = EventDispatcher()
    dispatcher.register(Probe)
    await dispatcher.dispatch(make_event(make_body))

    assert seen["received_at"].tzinfo is not None
    assert seen["merchant_id"] == 10001
