"""arq 任务 worker：调度投递、订阅执行、补卡任务、回调重试。

对应原来的 tests/test_task_worker.py：业务断言不变，调用方式从
“跑一圈进程内循环”改成“dispatcher 投递 → actor 执行”。
"""

import logging
from datetime import timedelta
from decimal import Decimal

import httpx
from sqlmodel import select

from app import crud
from app.integrations.ailink.errors import AiLinkBusinessError
from app.jobs import arq_worker
from app.models import Card, utcnow
from app.services.card_stock import StockReport
from tests.factories import FakeClient, drain_queue, make_card, make_ctx, task_outbox, us_bin
from tests.test_subscriptions import FakeByoc, make_settings, new_task, order


def callback_client(status_code=200):
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "body": request.content})
        return httpx.Response(status_code, text="ok")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), captured


class RejectOnceClient(FakeClient):
    """第一次申请被平台明确拒绝（业务错误），之后恢复正常。"""

    async def apply_cards(self, request, *, request_id=None):
        if not self.applies:
            self.calls.append("apply")
            self.applies.append(request)
            raise AiLinkBusinessError("余额不足，开卡失败", status_code=400, request_id="req1")
        return await super().apply_cards(request, request_id=request_id)


async def test_dispatch_runs_subscription_and_delivers_callback(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, captured = callback_client()
    ctx = make_ctx(make_settings(), session_factory, byoc=FakeByoc(), callback_http=http)

    executed = await drain_queue(ctx)

    db_session.refresh(task)
    assert executed == 2  # 任务本身 + 成功后调度的回调
    assert task.status == "succeeded"
    assert task.callback_delivered is True
    assert len(captured) == 1
    await http.aclose()


async def test_run_task_fails_when_byoc_missing(db_session, session_factory):
    """dispatcher 不会投这类任务，但真被执行到时也必须给明确原因，不能静默重试。"""
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    row = task_outbox(db_session, task.id or 0, "run")[-1]
    now = utcnow() + timedelta(seconds=1)
    http, _ = callback_client()
    ctx = make_ctx(make_settings(), session_factory, byoc=None, callback_http=http)

    await arq_worker.run_task(ctx, task.id or 0, row.token or 0, row.id or 0, now=now)

    db_session.refresh(task)
    assert task.status == "failed"
    assert "未配置 BYOC" in (task.last_error or "")
    await http.aclose()


async def test_dispatch_runs_open_card_task(db_session, session_factory):
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611110000000001",
    )
    http, _ = callback_client()
    ctx = make_ctx(
        make_settings(),
        session_factory,
        ailink=FakeClient(usd="3000.00"),
        callback_http=http,
    )

    await drain_queue(ctx)

    db_session.refresh(task)
    assert task.status == "succeeded"
    cards = db_session.exec(select(Card)).all()
    assert any(card.plan == "plus" for card in cards)
    await http.aclose()


async def test_run_task_polls_order_with_byoc_task_id(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    task.byoc_task_id = "BTASK0001"
    task.payload_ciphertext = None
    task.status = "processing"
    crud.save_task(db_session, task)
    byoc = FakeByoc()
    byoc.status_result = order(task.request_id)
    http, _ = callback_client()
    ctx = make_ctx(make_settings(), session_factory, byoc=byoc, callback_http=http)

    await drain_queue(ctx)

    db_session.refresh(task)
    assert task.status == "succeeded"
    assert task.payment_status == "completed"
    assert task.callback_delivered is True
    await http.aclose()


async def test_open_card_finishes_when_card_already_exists(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611120900000001",
    )
    http, _ = callback_client()
    ctx = make_ctx(
        make_settings(),
        session_factory,
        ailink=FakeClient(usd="3000.00"),
        callback_http=http,
    )

    await drain_queue(ctx)

    db_session.refresh(task)
    assert task.status == "succeeded"
    assert task.last_error == "已有可用卡，无需补卡"
    await http.aclose()


async def test_open_card_waits_for_webhook_in_processing(db_session, session_factory):
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121000000001",
    )
    http, _ = callback_client()
    ctx = make_ctx(
        make_settings(),
        session_factory,
        ailink=FakeClient(usd="3000.00", deliver_immediately=False),
        callback_http=http,
    )

    await drain_queue(ctx)

    db_session.refresh(task)
    assert task.status == "processing"
    assert task.submit_attempts == 1
    assert "等待 webhook" in (task.last_error or "")
    runs = task_outbox(db_session, task.id or 0, "run")
    assert runs and runs[-1].run_at > utcnow()
    await http.aclose()


async def test_open_card_retries_with_new_request_no_after_rejection(db_session, session_factory):
    """平台拒绝（例如没预留 0.1 开卡费）后，下一轮必须真的重新申请且换新请求号。"""
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121000000002",
    )
    client = RejectOnceClient(usd="48.10")
    settings = make_settings(open_card_max_attempts=2)
    http, _ = callback_client()
    ctx = make_ctx(settings, session_factory, ailink=client, callback_http=http)
    now = utcnow()

    await drain_queue(ctx, now=now)
    db_session.refresh(task)
    assert task.status == "processing"
    assert task.submit_attempts == 1
    assert "开卡被平台拒绝" in (task.last_error or "")

    await drain_queue(ctx, now=now + timedelta(seconds=121))
    db_session.refresh(task)
    assert task.status == "succeeded"
    assert len(client.applies) == 2
    assert client.applies[0].request_no != client.applies[1].request_no
    await http.aclose()


async def test_open_card_waiting_for_webhook_does_not_burn_attempts(db_session, session_factory):
    """等 webhook 的轮次没有平台交互，不算重试次数，别把任务提前熬成 failed。"""
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121000000003",
    )
    client = FakeClient(usd="3000.00", deliver_immediately=False)
    settings = make_settings(open_card_max_attempts=2)
    http, _ = callback_client()
    ctx = make_ctx(settings, session_factory, ailink=client, callback_http=http)
    now = utcnow()

    for offset in (0, 121, 242):
        await drain_queue(ctx, now=now + timedelta(seconds=offset))

    db_session.refresh(task)
    assert task.status == "processing"
    assert task.submit_attempts == 1
    assert "等待 webhook" in (task.last_error or "")
    assert client.calls.count("apply") == 1
    await http.aclose()


async def test_open_card_fails_after_max_attempts(db_session, session_factory):
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121100000001",
    )
    # 没有 5378 的美国 BIN：每轮都补不到卡，达到上限后必须失败
    client = FakeClient(usd="3000.00", bins=[us_bin(bin_number="486880")])
    settings = make_settings(open_card_max_attempts=2)
    http, _ = callback_client()
    ctx = make_ctx(settings, session_factory, ailink=client, callback_http=http)
    now = utcnow()

    await drain_queue(ctx, now=now)
    db_session.refresh(task)
    assert task.status == "processing"
    assert "BIN" in (task.last_error or "")

    await drain_queue(ctx, now=now + timedelta(seconds=121))
    db_session.refresh(task)
    assert task.status == "failed"
    assert task.submit_attempts == 2
    assert "不再补卡" in (task.last_error or "")
    await http.aclose()


async def test_open_card_fails_when_quota_exhausted(db_session, session_factory):
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121300000001",
    )
    http, _ = callback_client()
    ctx = make_ctx(
        make_settings(open_card_max_attempts=1),
        session_factory,
        ailink=FakeClient(usd="3000.00", quota_remaining=0),
        callback_http=http,
    )

    await drain_queue(ctx)

    db_session.refresh(task)
    assert task.status == "failed"
    assert "配额" in (task.last_error or "")
    await http.aclose()


async def test_open_card_fails_when_initial_funding_failed(db_session, session_factory):
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121400000001",
    )
    http, _ = callback_client()
    ctx = make_ctx(
        make_settings(open_card_max_attempts=1),
        session_factory,
        ailink=FakeClient(usd="3000.00", initial_funding_status="FAILED"),
        callback_http=http,
    )

    await drain_queue(ctx)

    db_session.refresh(task)
    assert task.status == "failed"
    assert "首充失败" in (task.last_error or "")
    await http.aclose()


async def test_dispatch_skips_tasks_not_due(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, _ = callback_client()
    ctx = make_ctx(make_settings(), session_factory, byoc=FakeByoc(), callback_http=http)

    executed = await drain_queue(ctx, now=utcnow() - timedelta(hours=1))

    db_session.refresh(task)
    assert executed == 0
    assert task.status == "pending"
    await http.aclose()


async def test_callback_is_retried_after_failure(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, _ = callback_client(status_code=500)
    ctx = make_ctx(
        make_settings(subscription_callback_max_attempts=3),
        session_factory,
        byoc=FakeByoc(),
        callback_http=http,
    )

    await drain_queue(ctx)

    db_session.refresh(task)
    assert task.status == "succeeded"
    assert task.callback_delivered is False
    assert task.callback_attempts == 1
    pending = task_outbox(db_session, task.id or 0, "callback")
    assert pending and pending[-1].run_at > utcnow()
    await http.aclose()


async def test_outbox_rows_drive_every_retry(db_session, session_factory):
    """每一次“以后再来”都落一条 outbox，不再依赖 next_run_at。"""
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611170000000021",
    )
    # 建任务就带了一条 outbox，token 是 1
    rows = task_outbox(db_session, task.id or 0, "run")
    assert len(rows) == 1 and rows[0].token == 1

    task.attempts += 1
    crud.save_task_with_outbox(
        db_session,
        task,
        kind=crud.OUTBOX_RUN,
        run_at=utcnow(),
        token=task.attempts,
    )

    rows = task_outbox(db_session, task.id or 0, "run")
    assert [row.token for row in rows] == [1, 2]


async def test_run_task_logs_start_and_finish(db_session, session_factory, caplog):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, _ = callback_client()
    ctx = make_ctx(make_settings(), session_factory, byoc=FakeByoc(), callback_http=http)

    with caplog.at_level(logging.INFO, logger="app.jobs.arq_worker"):
        await drain_queue(ctx)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "任务开始 task=" in message and "kind=open_subscription" in message for message in messages
    )
    assert any(
        f"任务结束 task={task.id}" in message and "status=succeeded" in message
        for message in messages
    )
    await http.aclose()


def test_task_status_counts_and_running_tasks(db_session):
    before = crud.task_status_counts(db_session)
    pending = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121300000001",
    )
    running = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121300000002",
    )
    running.status = "running"
    crud.save_task(db_session, running)

    counts = crud.task_status_counts(db_session)
    assert counts.get("pending", 0) == before.get("pending", 0) + 1
    assert counts.get("running", 0) == before.get("running", 0) + 1
    assert crud.pending_outbox_count(db_session) >= 0
    assert pending.id is not None


async def test_card_stock_scan_replenishes_and_logs(session_factory, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.jobs.arq_worker")
    seen: list[dict] = []

    async def fake_replenish(session, client, **kwargs):
        seen.append(kwargs)
        return StockReport(missing=["plus"], applied=["plus"])

    monkeypatch.setattr(arq_worker, "replenish", fake_replenish)
    ctx = make_ctx(make_settings(card_stock_enabled=True), session_factory, ailink=object())

    await arq_worker.card_stock_scan(ctx, now=utcnow())

    assert seen and seen[0]["usdt_rate"] == Decimal("0.997")
    assert "卡库存扫描完成" in caplog.text
    assert "已申请=plus" in caplog.text


async def test_card_stock_scan_skips_without_client(session_factory, caplog):
    caplog.set_level(logging.ERROR, logger="app.jobs.arq_worker")
    ctx = make_ctx(make_settings(card_stock_enabled=True), session_factory, ailink=None)

    await arq_worker.card_stock_scan(ctx)

    assert "缺少 AiLink 客户端" in caplog.text
