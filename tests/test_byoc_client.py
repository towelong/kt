"""BYOC async 客户端：报价、校验、建单、恢复与超时。"""

import json

import httpx
import pytest

from app.integrations.byoc import ByocClient, OrderValidationError, OutcomeUnknown, PollTimeout
from app.integrations.byoc.errors import APIError

API_KEY = "byoc_test_key"


def make_client(handler, limiter=None) -> ByocClient:
    async def no_sleep(_seconds):
        return None

    return ByocClient(
        API_KEY,
        "https://wangbohui.com",
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
        jitter=lambda: 0,
        limiter=limiter,
    )


def quote_payload(**overrides):
    payload = {
        "ok": True,
        "quote_id": "gxq1.test",
        "plan_type": "pro5",
        "authorized_amount_minor": 9000,
        "authorized_currency": "USD",
        "billing_country": "eg",
        "display_amount": "USD $90.00",
        "pricing_version": "v1",
        "issued_at": "2026-10-06T10:00:00+08:00",
        "valid_until": "2099-10-06T10:15:00+08:00",
    }
    payload.update(overrides)
    return payload


def order(
    request_id="order-2026100200012321",
    *,
    state="completed",
    settlement="PAID",
    success=True,
):
    return {
        "task_id": "TASK0001",
        "request_id": request_id,
        "payment_status": state,
        "settlement": settlement,
        "recharge_result": "success" if success else "pending",
        "payment_success": success,
        "polling_required": not success,
    }


def order_response(request_id, **kwargs):
    return {"ok": True, "order": order(request_id, **kwargs)}


async def test_quote_reads_amount_and_expiry():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/external/quotes/pro5"
        assert request.url.params["billing_country"] == "eg"
        return httpx.Response(200, json=quote_payload())

    async with make_client(handler) as client:
        quote = await client.quote("pro5", "EG")

    assert quote.quote_id == "gxq1.test"
    assert quote.authorized_amount_minor == 9000
    assert quote.display_amount == "USD $90.00"
    assert quote.plan_type == "pro5"


async def test_quote_with_plan_mismatch_times_out_instead_of_using_it():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=quote_payload(plan_type="plus"))

    async with make_client(handler) as client:
        with pytest.raises(PollTimeout):
            await client.quote("pro5", "eg")


async def test_validate_then_create_sends_same_order():
    request_id = "order-2026100200012321"
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/orders/validate"):
            return httpx.Response(
                200,
                json={"ok": True, "idempotent_replay": False, "order": order(request_id)},
            )
        assert request.url.path == "/api/v1/external/orders"
        return httpx.Response(201, json=order_response(request_id))

    payload = {"request_id": request_id, "plan_type": "pro5", "billing_country": "eg"}
    async with make_client(handler) as client:
        result = await client.create_order(payload)

    assert [path for path, _ in seen] == [
        "/api/v1/external/orders/validate",
        "/api/v1/external/orders",
    ]
    assert seen[0][1] == seen[1][1] == payload
    assert result["task_id"] == "TASK0001"
    assert result["payment_success"] is True


async def test_validate_accepts_preflight_response_without_order():
    request_id = "order-2026100200012331"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/external/orders/validate"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "valid": True,
                "request_id": request_id,
                "plan_type": "pro5",
                "billing_country": "eg",
                "quote_bound": True,
                "idempotent_replay": False,
                "would_create_new_order": True,
                "accepting_orders": True,
                "account_preflight_performed": True,
            },
        )

    async with make_client(handler) as client:
        result = await client.validate_order(
            {"request_id": request_id, "plan_type": "pro5", "billing_country": "eg"}
        )

    assert result.valid is True
    assert result.quote_bound is True
    assert result.order is None


async def test_validate_rejects_invalid_preflight():
    request_id = "order-2026100200012332"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "valid": False,
                "request_id": request_id,
                "plan_type": "pro5",
                "billing_country": "eg",
                "quote_bound": False,
                "accepting_orders": True,
            },
        )

    async with make_client(handler) as client:
        with pytest.raises(OrderValidationError) as info:
            await client.validate_order(
                {"request_id": request_id, "plan_type": "pro5", "billing_country": "eg"}
            )

    assert "valid != true" in str(info.value)


async def test_create_conflict_recovers_original_order():
    request_id = "order-2026100200012322"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"detail": {"code": "CHANGED_BODY"}})
        assert request.url.path == f"/api/v1/external/orders/by-request/{request_id}"
        return httpx.Response(200, json=order_response(request_id))

    async with make_client(handler) as client:
        with pytest.raises(APIError) as info:
            await client.create("/orders", {"request_id": request_id})

    assert info.value.status == 409
    assert info.value.recovered_task_id == "TASK0001"


class RecordingLimiter:
    def __init__(self):
        self.calls = 0

    async def acquire(self, *, max_wait=None):
        self.calls += 1


async def test_every_http_attempt_consumes_a_rate_limit_slot():
    request_id = "order-2026100200012329"
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            if posts == 1:
                return httpx.Response(
                    429,
                    json={"detail": {"code": "RATE_LIMIT"}},
                    headers={"Retry-After": "0"},
                )
            return httpx.Response(201, json=order_response(request_id))
        return httpx.Response(404, json={"detail": {"code": "NOT_FOUND"}})

    limiter = RecordingLimiter()
    async with make_client(handler, limiter=limiter) as client:
        await client.create("/orders", {"request_id": request_id})

    # 首次 POST + recover GET + 重试 POST
    assert limiter.calls == 3


async def test_create_retries_429_with_same_request_id():
    request_id = "order-2026100200012323"
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content)["request_id"])
            if len(posts) == 1:
                return httpx.Response(
                    429,
                    json={"detail": {"code": "RATE_LIMIT"}},
                    headers={"Retry-After": "0"},
                )
            return httpx.Response(201, json=order_response(request_id))
        return httpx.Response(404, json={"detail": {"code": "NOT_FOUND"}})

    async with make_client(handler) as client:
        result = await client.create("/orders", {"request_id": request_id})

    assert result["task_id"] == "TASK0001"
    assert posts == [request_id, request_id]


async def test_create_outcome_unknown_does_not_retry_with_new_id():
    request_id = "order-2026100200012324"
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(404, json={"detail": {"code": "NOT_FOUND"}})

    async with make_client(handler) as client:
        with pytest.raises(OutcomeUnknown) as info:
            await client.create("/orders", {"request_id": request_id}, max_attempts=1)

    assert info.value.request_id == request_id
    assert posts == 1


async def test_status_rejects_inconsistent_order():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "order": order(state="completed", settlement="NONE", success=True),
            },
        )

    async with make_client(handler) as client:
        with pytest.raises(PollTimeout):
            await client.status("TASK0001")


async def test_status_returns_order():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/external/orders/task0001"
        return httpx.Response(200, json=order_response(order()["request_id"]))

    async with make_client(handler) as client:
        result = await client.status("task0001")

    assert result["task_id"] == "TASK0001"
