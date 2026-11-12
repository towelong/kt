"""订阅任务：加密落库、选卡锁卡、BYOC 下单、回调签名与重试。"""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import crud
from app.config import Settings, get_settings
from app.db import get_session
from app.integrations.byoc import OutcomeUnknown
from app.integrations.byoc.client import Quote
from app.integrations.byoc.errors import APIError, OrderValidationError, PollTimeout
from app.models import utcnow
from app.routers.subscriptions import router as subscriptions_router
from app.services.subscriptions import (
    CALLBACK_HEADER,
    KIND_OPEN_CARD,
    build_callback_body,
    create_subscription_task,
    deliver_callback,
    parse_card_expiry,
    process_subscription_task,
    sign_callback,
)
from tests.factories import make_card

PAYLOAD_KEY = "test-payload-key"
CALLBACK_SECRET = "cb-secret"
SESSION = {
    "accessToken": "access-token-value",
    "sessionToken": "session-token-value",
    "user": {"id": "u1", "email": "user@example.com"},
    "account": {"id": "a1"},
}


def make_settings(**overrides) -> Settings:
    defaults = {
        "_env_file": None,
        "subscription_worker_enabled": False,
        "task_payload_key": PAYLOAD_KEY,
        "byoc_callback_secret": CALLBACK_SECRET,
        "subscription_callback_max_attempts": 3,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def quote(plan_type="plus", *, amount=2000, valid_for=600) -> Quote:
    return Quote(
        quote_id="gxq1.test",
        plan_type=plan_type,
        billing_country="ph",
        authorized_amount_minor=amount,
        authorized_currency="USD",
        display_amount=f"USD ${amount / 100:.2f}",
        valid_until=utcnow() + timedelta(seconds=valid_for),
    )


def order(request_id: str, *, state="completed", settlement="PAID", success=True):
    return {
        "task_id": "BTASK0001",
        "request_id": request_id,
        "payment_status": state,
        "settlement": settlement,
        "recharge_result": "success" if success else "pending",
        "payment_success": success,
        "polling_required": not success,
    }


class FakeByoc:
    def __init__(self, *, quote_result=None, order_state=None, create_error=None):
        self.quote_result = quote_result or quote()
        self.order_state = order_state or order("unused")
        self.create_error = create_error
        self.quotes: list[tuple[str, str | None]] = []
        self.validated: list[dict] = []
        self.created: list[dict] = []
        self.status_result = None

    async def quote(self, plan_type, billing_country=None, *, timeout=30):
        self.quotes.append((plan_type, billing_country))
        return self.quote_result

    async def validate_order(self, order, *, timeout=60):
        self.validated.append(order)
        return order

    async def create(self, path, order, *, timeout=120):
        if self.create_error is not None:
            raise self.create_error
        self.created.append(order)
        result = dict(self.order_state)
        result["request_id"] = order["request_id"]
        return result

    async def status(self, task_id, *, timeout=60):
        result = dict(self.status_result or self.order_state)
        result["request_id"] = result.get("request_id") or "unused"
        return result


def new_task(session, plan="plus", callback="https://merchant.example/cb"):
    return create_subscription_task(
        session,
        plan=plan,
        session_payload=SESSION,
        callback_url=callback,
        settings=make_settings(),
    )


def test_create_task_encrypts_session(db_session):
    task = new_task(db_session)

    assert task.payload_ciphertext
    assert SESSION["accessToken"] not in task.payload_ciphertext
    assert "accessToken" not in task.payload_ciphertext
    from app.services.task_payload import decrypt_payload

    payload = decrypt_payload(task.payload_ciphertext, PAYLOAD_KEY)
    assert payload["session"]["accessToken"] == SESSION["accessToken"]
    assert payload["plan"] == "plus"


async def test_without_card_task_waits_and_enqueues_card_opening(db_session):
    task = new_task(db_session, plan="pro200")
    byoc = FakeByoc(quote_result=quote(plan_type="pro20", amount=9000))

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    assert task.status == "waiting_card"
    assert byoc.created == []
    open_card = crud.find_open_card_task(db_session, "pro200")
    assert open_card is not None
    assert open_card.kind == KIND_OPEN_CARD


async def test_successful_subscription_builds_order_and_locks_then_releases_card(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)
    byoc = FakeByoc()

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "succeeded"
    assert task.byoc_task_id == "BTASK0001"
    assert task.payment_status == "completed"
    assert task.display_amount == "USD $20.00"
    assert task.payload_ciphertext is None
    assert card.reserved_until is None and card.reserved_task_id is None
    assert task.callback_next_at is not None

    assert byoc.quotes == [("plus", "ph")]
    sent = byoc.created[0]
    assert sent["plan_type"] == "plus"
    assert sent["billing_country"] == "ph"
    assert sent["bill_name"] == "plus"
    assert sent["quote_id"] == "gxq1.test"
    assert sent["auto_cancel_renew"] is True
    assert sent["card_number"] == card.card_no
    assert (sent["exp_month"], sent["exp_year"]) == (12, 2029)
    assert sent["cvv"] == "123"
    assert sent["session"]["accessToken"] == SESSION["accessToken"]


async def test_expired_quote_requeues_without_locking_card(db_session):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    byoc = FakeByoc(quote_result=quote(valid_for=-1))

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    assert task.status == "waiting_quote"
    assert task.card_id is None
    assert byoc.created == []


async def test_byoc_rejection_fails_and_releases_card(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)
    byoc = FakeByoc(create_error=APIError(400, {"message": "卡被拒绝"}))

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "failed"
    assert "卡被拒绝" in (task.last_error or "")
    assert task.payload_ciphertext is None
    assert task.card_id == card.id
    assert card.reserved_until is None


async def test_unknown_outcome_keeps_card_locked(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)
    byoc = FakeByoc(create_error=OutcomeUnknown(task.request_id))

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "unknown"
    assert task.card_id == card.id
    assert card.reserved_until is not None
    assert task.callback_next_at is not None


def test_callback_signature_and_body():
    body = {"status": "succeeded", "task_id": "7", "request_id": "order-1"}
    expected = hashlib.md5(
        ("request_id=order-1&status=succeeded&task_id=7" + CALLBACK_SECRET).encode()
    ).hexdigest()
    assert sign_callback(body, CALLBACK_SECRET) == expected


def test_parse_card_expiry():
    assert parse_card_expiry("11/28") == (11, 2028)
    assert parse_card_expiry("01/30") == (1, 2030)
    assert parse_card_expiry("13/28") is None
    assert parse_card_expiry("") is None
    assert parse_card_expiry("12-29") is None


async def test_callback_is_signed_and_delivered(db_session):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    await process_subscription_task(db_session, task, byoc=FakeByoc(), settings=make_settings())
    db_session.refresh(task)

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["signature"] = request.headers[CALLBACK_HEADER]
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        delivered = await deliver_callback(
            db_session, task, http_client=http, settings=make_settings()
        )

    assert delivered is True
    db_session.refresh(task)
    assert task.callback_delivered is True
    body = captured["body"]
    assert body["task_id"] == str(task.id)
    assert body["status"] == "succeeded"
    assert captured["signature"] == sign_callback(build_callback_body(task), CALLBACK_SECRET)


async def test_callback_retries_then_gives_up(db_session):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    await process_subscription_task(db_session, task, byoc=FakeByoc(), settings=make_settings())
    db_session.refresh(task)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="no")

    settings = make_settings(subscription_callback_max_attempts=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        delivered = await deliver_callback(db_session, task, http_client=http, settings=settings)
        assert delivered is False
        db_session.refresh(task)
        assert task.callback_attempts == 1
        assert task.callback_next_at is not None

        later = utcnow() + timedelta(minutes=10)
        assert (
            await deliver_callback(db_session, task, http_client=http, settings=settings, now=later)
            is False
        )

    db_session.refresh(task)
    assert task.callback_attempts == 2
    assert task.callback_next_at is None
    assert task.callback_delivered is False


# --------------------------------------------------------------------------- 接口


def build_app(session, settings):
    api = FastAPI()
    api.include_router(subscriptions_router, prefix="/api")
    api.dependency_overrides[get_session] = lambda: session
    api.dependency_overrides[get_settings] = lambda: settings
    return TestClient(api)


def test_endpoint_accepts_task_without_leaking_session(db_session, api_key):
    client = build_app(db_session, make_settings())

    response = client.post(
        "/api/subscriptions",
        json={"plan": "plus", "session": SESSION, "callback_url": "https://m.example/cb"},
        headers=api_key[1],
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert "session" not in response.text
    task = crud.get_task(db_session, body["task_id"])
    assert task.request_id == body["request_id"]

    status = client.get(f"/api/subscriptions/{body['task_id']}", headers=api_key[1])
    assert status.status_code == 200
    assert status.json()["status"] == "pending"
    assert SESSION["accessToken"] not in status.text


def test_endpoint_rejects_unknown_plan(db_session, api_key):
    client = build_app(db_session, make_settings())
    response = client.post(
        "/api/subscriptions",
        json={"plan": "nope", "session": SESSION},
        headers=api_key[1],
    )
    assert response.status_code == 422


def test_endpoint_requires_payload_key(db_session, api_key):
    client = build_app(db_session, make_settings(task_payload_key=None))
    response = client.post(
        "/api/subscriptions",
        json={"plan": "plus", "session": SESSION},
        headers=api_key[1],
    )
    assert response.status_code == 503


def test_endpoint_rejects_bad_callback_url(db_session, api_key):
    client = build_app(db_session, make_settings())
    response = client.post(
        "/api/subscriptions",
        json={"plan": "plus", "session": SESSION, "callback_url": "ftp://x"},
        headers=api_key[1],
    )
    assert response.status_code == 422


def test_get_missing_task_returns_404(db_session, api_key):
    client = build_app(db_session, make_settings())
    assert client.get("/api/subscriptions/9999", headers=api_key[1]).status_code == 404


@pytest.mark.parametrize("plan", ["plus", "pro100", "pro200", "pro500"])
def test_every_plan_maps_to_byoc_plan_type(plan):
    from app.services.card_stock import PLAN_BY_NAME

    spec = PLAN_BY_NAME[plan]
    assert spec.byoc_plan_type
    assert spec.billing_country in {"ph", "eg"}


async def test_validation_rejection_fails_without_charging(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)

    class RejectingByoc(FakeByoc):
        async def validate_order(self, order, *, timeout=60):
            raise APIError(422, {"message": "session 不合法"})

    await process_subscription_task(
        db_session, task, byoc=RejectingByoc(), settings=make_settings()
    )

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "failed"
    assert "session 不合法" in (task.last_error or "")
    assert task.payload_ciphertext is None
    assert card.reserved_until is None


async def test_reuses_card_locked_by_same_task(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)
    now = utcnow()
    assert crud.claim_card(
        db_session,
        card.id,
        task.request_id,
        until=now + timedelta(minutes=5),
        now=now,
    )
    task.card_id = card.id
    crud.save_task(db_session, task)

    byoc = FakeByoc()
    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "succeeded"
    assert task.card_id == card.id
    assert byoc.created[0]["card_number"] == card.card_no
    assert card.reserved_until is None


async def test_validation_error_fails_with_reason(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)

    class InvalidByoc(FakeByoc):
        async def validate_order(self, order, *, timeout=60):
            raise OrderValidationError("quote_bound=false", detail={"quote_bound": False})

    await process_subscription_task(db_session, task, byoc=InvalidByoc(), settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "failed"
    assert "quote_bound=false" in (task.last_error or "")
    assert task.payload_ciphertext is None
    assert card.reserved_until is None


async def test_validate_timeout_releases_card(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)

    class TimeoutByoc(FakeByoc):
        async def validate_order(self, order, *, timeout=60):
            raise PollTimeout("/orders/validate")

    await process_subscription_task(db_session, task, byoc=TimeoutByoc(), settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "pending"
    assert task.card_id == card.id
    assert task.payload_ciphertext is not None
    assert card.reserved_until is None and card.reserved_task_id is None


async def test_validate_network_error_releases_card(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)

    class NetworkByoc(FakeByoc):
        async def validate_order(self, order, *, timeout=60):
            raise httpx.ConnectError("boom")

    await process_subscription_task(db_session, task, byoc=NetworkByoc(), settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "pending"
    assert task.card_id == card.id
    assert card.reserved_until is None


async def test_conflict_recovery_uses_original_order(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)

    class ConflictByoc(FakeByoc):
        async def create(self, path, order, *, timeout=120):
            error = APIError(409, {"message": "重复提交"})
            error.recovered_task_id = "BTASK0001"
            raise error

    byoc = ConflictByoc()
    byoc.status_result = order(task.request_id)

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "succeeded"
    assert task.byoc_task_id == "BTASK0001"
    assert card.reserved_until is None


async def test_quote_amount_is_not_validated(db_session):
    card = make_card(db_session, plan="plus")
    task = new_task(db_session)
    byoc = FakeByoc(quote_result=quote(amount=999900))

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "succeeded"
    assert task.authorized_amount_minor == 999900
    assert task.display_amount == "USD $9999.00"
    assert byoc.created and byoc.created[0]["quote_id"] == "gxq1.test"
    assert card.reserved_until is None


async def test_card_balance_below_plan_unit_price_waits(db_session):
    card = make_card(db_session, plan="plus", balance=Decimal("15.99"))
    task = new_task(db_session)
    byoc = FakeByoc()

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "waiting_card"
    assert byoc.created == []
    assert crud.find_open_card_task(db_session, "plus") is not None
    assert card.reserved_until is None


async def test_card_balance_equal_to_plan_unit_price_is_used(db_session):
    card = make_card(db_session, plan="plus", balance=Decimal("16.00"))
    task = new_task(db_session)
    byoc = FakeByoc()

    await process_subscription_task(db_session, task, byoc=byoc, settings=make_settings())

    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "succeeded"
    assert byoc.created[0]["card_number"] == card.card_no
