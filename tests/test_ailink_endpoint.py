"""Webhook 端点：验签、应答 ok、幂等、失败返回 500 让平台重试。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.integrations.ailink.dispatch import EventDispatcher, EventHandler
from app.integrations.ailink.events import WebhookEvent
from app.routers.webhooks import get_dispatcher
from app.routers.webhooks import router as webhook_router

WEBHOOK_SECRET = "whsec_test_secret"
URL = "/webhooks/ailink"

# 只挂 webhook 路由，避免测试导入 app.main 时顺带初始化数据库引擎
webhook_app = FastAPI()
webhook_app.include_router(webhook_router)


class Recorder(EventHandler[WebhookEvent]):
    event_model = WebhookEvent
    categories = ("CARD_ISSUE",)

    def __init__(self, calls) -> None:
        self.calls = calls

    async def process(self, event: WebhookEvent, ctx) -> None:
        self.calls.append(event.event_type)


class Boom(EventHandler[WebhookEvent]):
    event_model = WebhookEvent
    event_types = ("CARD_ISSUE.SUCCESS",)

    async def process(self, event: WebhookEvent, ctx) -> None:
        raise RuntimeError("落库失败")


@pytest.fixture
def webhook():
    dispatcher = EventDispatcher()
    calls: list[str] = []

    class Recording(Recorder):
        def __init__(self) -> None:
            super().__init__(calls)

    dispatcher.register(Recording)
    settings = Settings(_env_file=None, ailink_webhook_secret=WEBHOOK_SECRET)
    webhook_app.dependency_overrides[get_settings] = lambda: settings
    webhook_app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    client = TestClient(webhook_app)
    yield SimpleNamespace(client=client, calls=calls, dispatcher=dispatcher, settings=settings)
    webhook_app.dependency_overrides.clear()


def test_valid_event_is_acked_with_plain_ok(webhook, sign_webhook, card_issue_body):
    headers = sign_webhook(card_issue_body, event_id="evt_card_issue_1")
    response = webhook.client.post(URL, content=card_issue_body, headers=headers)

    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["content-type"].startswith("text/plain")
    assert webhook.calls == ["CARD_ISSUE.SUCCESS"]


def test_duplicate_delivery_runs_handler_once(webhook, sign_webhook, card_issue_body):
    headers = sign_webhook(card_issue_body, event_id="evt_card_issue_1")
    assert webhook.client.post(URL, content=card_issue_body, headers=headers).status_code == 200
    assert webhook.client.post(URL, content=card_issue_body, headers=headers).status_code == 200

    assert webhook.calls == ["CARD_ISSUE.SUCCESS"]


def test_tampered_body_is_forbidden(webhook, sign_webhook, card_issue_body):
    headers = sign_webhook(card_issue_body, event_id="evt_card_issue_1")
    response = webhook.client.post(URL, content=card_issue_body + b" ", headers=headers)

    assert response.status_code == 403
    assert webhook.calls == []


def test_missing_signature_header_is_unauthorized(webhook, card_issue_body):
    response = webhook.client.post(URL, content=card_issue_body)

    assert response.status_code == 401


def test_invalid_payload_with_valid_signature_is_bad_request(
    webhook, sign_webhook, card_issue_body
):
    body = b'{"eventType":"CARD_ISSUE.SUCCESS"}'
    headers = sign_webhook(body, event_id="evt_bad")
    response = webhook.client.post(URL, content=body, headers=headers)

    assert response.status_code == 400


def test_unknown_event_type_is_acked(webhook, sign_webhook, card_issue_body):
    body = card_issue_body.replace(b"CARD_ISSUE.SUCCESS", b"CARD_FUTURE.NEW")
    body = body.replace(b"CARD_ISSUE", b"CARD_FUTURE")
    headers = sign_webhook(body, event_type="CARD_FUTURE.NEW", event_id="evt_future")

    response = webhook.client.post(URL, content=body, headers=headers)

    assert response.status_code == 200
    assert response.text == "ok"
    assert webhook.calls == []


def test_handler_failure_returns_500_for_retry(webhook, sign_webhook, card_issue_body):
    webhook.dispatcher.register(Boom)
    headers = sign_webhook(card_issue_body, event_id="evt_card_issue_1")

    response = webhook.client.post(URL, content=card_issue_body, headers=headers)

    assert response.status_code == 500


def test_missing_webhook_secret_refuses_to_process(webhook, sign_webhook, card_issue_body):
    webhook_app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    headers = sign_webhook(card_issue_body, event_id="evt_card_issue_1")

    response = webhook.client.post(URL, content=card_issue_body, headers=headers)

    assert response.status_code == 503


def test_oversized_body_is_rejected(webhook, sign_webhook, card_issue_body):
    webhook_app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        ailink_webhook_secret=WEBHOOK_SECRET,
        ailink_webhook_max_body_bytes=16,
    )
    headers = sign_webhook(card_issue_body, event_id="evt_card_issue_1")

    response = webhook.client.post(URL, content=card_issue_body, headers=headers)

    assert response.status_code == 413
