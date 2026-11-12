"""Webhook 端点：验签、应答 ok、幂等、失败返回 500 让平台重试。"""

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from app.config import Settings, get_settings
from app.integrations.ailink.dispatch import EventDispatcher, EventHandler
from app.integrations.ailink.events import WebhookEvent
from app.routers.webhooks import get_dispatcher, receive_ailink_webhook
from app.routers.webhooks import router as webhook_router
from tests.conftest import build_webhook_headers

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


class _DisconnectRequest:
    headers: dict = {}
    client = None

    async def body(self):
        raise ClientDisconnect()


async def test_webhook_client_disconnect_is_handled():
    response = await receive_ailink_webhook(
        _DisconnectRequest(),
        Settings(_env_file=None),
        EventDispatcher(),
    )
    assert response.status_code == 400
    assert response.body == b"client disconnected"


def test_rejected_request_logs_headers_and_redacted_body(webhook, caplog):
    caplog.set_level(logging.WARNING, logger="app.routers.webhooks")
    payload = {
        "eventId": "evt_log_1",
        "eventType": "CARD_3DS.OTP_RECEIVED",
        "eventCategory": "CARD_3DS",
        "eventTime": "2026-11-12 12:00:00",
        "apiVersion": "2026-06-01",
        "merchantId": 10001,
        "data": {"otpCode": "387123", "cardNoMask": "4865****1234"},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = build_webhook_headers(
        body,
        secret="wrong-secret",
        event_id="evt_log_1",
        event_type="CARD_3DS.OTP_RECEIVED",
    )

    response = webhook.client.post(URL, content=body, headers=headers)

    assert response.status_code == 403
    assert "VCC Webhook 验签失败" in caplog.text
    assert "evt_log_1" in caplog.text  # header 有记录
    assert '"otpCode":"***"' in caplog.text  # 敏感字段打码
    assert "387123" not in caplog.text


def test_debug_logs_successful_request(webhook, caplog, sign_webhook, card_issue_body):
    caplog.set_level(logging.INFO, logger="app.routers.webhooks")
    webhook_app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        ailink_webhook_secret=WEBHOOK_SECRET,
        ailink_webhook_debug=True,
    )
    headers = sign_webhook(card_issue_body, event_id="evt_card_issue_1")

    response = webhook.client.post(URL, content=card_issue_body, headers=headers)

    assert response.status_code == 200
    assert "VCC Webhook 收到请求" in caplog.text
    assert "x-vcc-webhook-id" in caplog.text
