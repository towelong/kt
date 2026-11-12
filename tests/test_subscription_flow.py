"""端到端：HTTP 提交订阅 → 任务入库 → worker 选卡 → 真实 ByocClient（MockTransport）→ 回调。

不 mock 业务层，只把 BYOC 和回调的 HTTP 用 MockTransport 接住，验证整条链路。
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from app import crud
from app.integrations.byoc.client import ByocClient
from app.jobs.task_worker import run_once
from app.models import utcnow
from app.services.subscriptions import sign_callback
from tests.factories import make_card
from tests.test_subscriptions import (
    CALLBACK_SECRET,
    SESSION,
    build_app,
    make_settings,
)

QUOTE_AMOUNT_MINOR = 2000
CARD_OK = "VC_FLOW_OK"


class RecordingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self, *, max_wait=None) -> None:
        self.calls += 1


def order_payload(request_id: str, *, success=True) -> dict:
    return {
        "task_id": "BTASK_FLOW_1",
        "request_id": request_id,
        "payment_status": "completed" if success else "failed",
        "settlement": "PAID" if success else "NONE",
        "recharge_result": "success" if success else "failed",
        "payment_success": success,
        "polling_required": False,
    }


def quote_payload() -> dict:
    return {
        "ok": True,
        "quote_id": "gxq1.flow",
        "plan_type": "plus",
        "authorized_amount_minor": QUOTE_AMOUNT_MINOR,
        "authorized_currency": "USD",
        "billing_country": "ph",
        "display_amount": "USD $20.00",
        "pricing_version": "v1",
        "issued_at": datetime.now(UTC).isoformat(),
        "valid_until": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }


def byoc_handler(order_log: list[dict], paths: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            assert request.url.path == "/api/v1/external/quotes/plus"
            assert request.url.params["billing_country"] == "ph"
            return httpx.Response(200, json=quote_payload())

        body = json.loads(request.content)
        if request.url.path.endswith("/orders/validate"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "idempotent_replay": False,
                    "order": order_payload(body["request_id"]),
                },
            )
        assert request.url.path == "/api/v1/external/orders"
        order_log.append(body)
        return httpx.Response(201, json={"ok": True, "order": order_payload(body["request_id"])})

    return handler


def callback_handler(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["signature"] = request.headers["Kt-Secret"]
        return httpx.Response(200, text="ok")

    return handler


async def test_subscription_end_to_end(db_session, session_factory, api_key):
    card_ok = make_card(
        db_session,
        plan="plus",
        card_id=CARD_OK,
        request_no="kaika_plus_flow_ok",
        balance=Decimal("48.00"),
    )
    settings = make_settings()

    client = build_app(db_session, settings)
    response = client.post(
        "/api/subscriptions",
        json={
            "plan": "plus",
            "session": SESSION,
            "callback_url": "https://merchant.example/cb",
        },
        headers=api_key[1],
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]

    order_log: list[dict] = []
    paths: list[str] = []
    limiter = RecordingLimiter()
    byoc = ByocClient(
        "byoc_key",
        "https://wangbohui.com",
        transport=httpx.MockTransport(byoc_handler(order_log, paths)),
        limiter=limiter,
    )
    captured: dict = {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(callback_handler(captured))
    ) as callback_http:
        processed = await run_once(
            session_factory=session_factory,
            settings=settings,
            byoc=byoc,
            ailink=None,
            callback_http=callback_http,
            now=utcnow(),
        )

    await byoc.aclose()

    # 任务走完：报价 → 校验 → 下单，每个 HTTP 请求占一个限流配额
    assert processed == 1
    assert paths == [
        "GET /api/v1/external/quotes/plus",
        "POST /api/v1/external/orders/validate",
        "POST /api/v1/external/orders",
    ]
    assert limiter.calls == 3

    task = crud.get_task(db_session, task_id)
    db_session.refresh(task)
    assert task.status == "succeeded"
    assert task.byoc_task_id == "BTASK_FLOW_1"
    assert task.display_amount == "USD $20.00"
    assert task.payload_ciphertext is None
    assert task.callback_delivered is True

    # 订单里用的是好卡，不是余额不足那张
    assert len(order_log) == 1
    sent = order_log[0]
    assert sent["request_id"] == task.request_id
    assert sent["plan_type"] == "plus"
    assert sent["billing_country"] == "ph"
    assert sent["bill_name"] == "plus"
    assert sent["quote_id"] == "gxq1.flow"
    assert sent["card_number"] == card_ok.card_no
    assert (sent["exp_month"], sent["exp_year"]) == (12, 2029)
    assert sent["cvv"] == "123"
    assert sent["session"]["accessToken"] == SESSION["accessToken"]
    assert sent["auto_cancel_renew"] is True

    # 卡锁释放，另一张没被动过
    db_session.refresh(card_ok)
    assert card_ok.reserved_until is None and card_ok.reserved_task_id is None

    # 回调签名和内容
    body = captured["body"]
    assert body["task_id"] == str(task.id)
    assert body["request_id"] == task.request_id
    assert body["status"] == "succeeded"
    assert body["byoc_task_id"] == "BTASK_FLOW_1"
    assert captured["signature"] == sign_callback(body, CALLBACK_SECRET)

    # session 不应出现在任何响应/回调/数据库明文里
    assert SESSION["accessToken"] not in json.dumps(body)
    assert task.payload_ciphertext is None


async def test_subscription_end_to_end_byoc_failure(db_session, session_factory, api_key):
    card = make_card(
        db_session,
        plan="plus",
        card_id="VC_FLOW_FAIL",
        request_no="kaika_plus_flow_fail",
    )
    settings = make_settings()
    client = build_app(db_session, settings)
    response = client.post(
        "/api/subscriptions",
        json={"plan": "plus", "session": SESSION, "callback_url": "https://merchant.example/cb"},
        headers=api_key[1],
    )
    task_id = response.json()["task_id"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=quote_payload())
        body = json.loads(request.content)
        if request.url.path.endswith("/orders/validate"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "idempotent_replay": False,
                    "order": order_payload(body["request_id"]),
                },
            )
        return httpx.Response(
            201,
            json={"ok": True, "order": order_payload(body["request_id"], success=False)},
        )

    byoc = ByocClient(
        "byoc_key",
        "https://wangbohui.com",
        transport=httpx.MockTransport(handler),
    )
    captured: dict = {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(callback_handler(captured))
    ) as callback_http:
        await run_once(
            session_factory=session_factory,
            settings=settings,
            byoc=byoc,
            ailink=None,
            callback_http=callback_http,
            now=utcnow(),
        )
    await byoc.aclose()

    task = crud.get_task(db_session, task_id)
    db_session.refresh(task)
    db_session.refresh(card)
    assert task.status == "failed"
    assert task.payment_status == "failed"
    assert card.reserved_until is None
    assert captured["body"]["status"] == "failed"
    assert captured["signature"] == sign_callback(captured["body"], CALLBACK_SECRET)


async def test_subscription_endpoint_without_card_enqueues_opening(
    db_session, session_factory, api_key
):
    settings = make_settings()
    client = build_app(db_session, settings)
    response = client.post(
        "/api/subscriptions",
        json={"plan": "plus", "session": SESSION, "callback_url": "https://merchant.example/cb"},
        headers=api_key[1],
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json=quote_payload())

    byoc = ByocClient(
        "byoc_key",
        "https://wangbohui.com",
        transport=httpx.MockTransport(handler),
    )
    captured: dict = {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(callback_handler(captured))
    ) as callback_http:
        await run_once(
            session_factory=session_factory,
            settings=settings,
            byoc=byoc,
            ailink=None,
            callback_http=callback_http,
            now=utcnow(),
        )
    await byoc.aclose()

    task = crud.get_task(db_session, task_id)
    db_session.refresh(task)
    # 只取了报价，没有建单；任务等待补卡，并投递了 open_card 任务
    assert paths == ["GET /api/v1/external/quotes/plus"]
    assert task.status == "waiting_card"
    open_card = crud.find_open_card_task(db_session, "plus")
    assert open_card is not None
    assert open_card.kind == "open_card"
    assert captured == {}
