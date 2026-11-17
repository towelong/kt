"""任务 worker：认领、订阅执行、补卡任务、回调重试。"""

import logging
from datetime import timedelta

import httpx
from sqlmodel import select

from app import crud
from app.integrations.ailink.errors import AiLinkBusinessError
from app.jobs.task_worker import run_once
from app.models import Card, utcnow
from tests.factories import FakeClient, make_card, us_bin
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


async def test_run_once_processes_subscription_and_delivers_callback(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, captured = callback_client()

    processed = await run_once(
        session_factory=session_factory,
        settings=make_settings(),
        byoc=FakeByoc(),
        ailink=None,
        callback_http=http,
        now=utcnow(),
    )

    db_session.refresh(task)
    assert processed == 1
    assert task.status == "succeeded"
    assert task.callback_delivered is True
    assert len(captured) == 1

    await http.aclose()


async def test_run_once_without_byoc_fails_task(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, _ = callback_client()

    await run_once(
        session_factory=session_factory,
        settings=make_settings(),
        byoc=None,
        ailink=None,
        callback_http=http,
        now=utcnow(),
    )

    db_session.refresh(task)
    assert task.status == "failed"
    assert "未配置 BYOC" in (task.last_error or "")
    await http.aclose()


async def test_run_once_executes_open_card_task(db_session, session_factory):
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611110000000001",
    )
    http, _ = callback_client()

    await run_once(
        session_factory=session_factory,
        settings=make_settings(),
        byoc=None,
        ailink=FakeClient(usd="3000.00"),
        callback_http=http,
        now=utcnow(),
    )

    db_session.refresh(task)
    assert task.status == "succeeded"
    cards = db_session.exec(select(Card)).all()
    assert any(card.plan == "plus" for card in cards)
    await http.aclose()


async def test_run_once_polls_task_with_byoc_task_id_instead_of_reading_payload(
    db_session, session_factory
):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    task.byoc_task_id = "BTASK0001"
    task.payload_ciphertext = None
    task.status = "processing"
    crud.save_task(db_session, task)
    byoc = FakeByoc()
    byoc.status_result = order(task.request_id)
    http, _ = callback_client()

    await run_once(
        session_factory=session_factory,
        settings=make_settings(),
        byoc=byoc,
        ailink=None,
        callback_http=http,
        now=utcnow(),
    )

    db_session.refresh(task)
    assert task.status == "succeeded"
    assert task.payment_status == "completed"
    assert task.callback_delivered is True
    await http.aclose()


async def test_open_card_task_finishes_when_card_already_exists(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611120900000001",
    )
    http, _ = callback_client()

    await run_once(
        session_factory=session_factory,
        settings=make_settings(),
        byoc=None,
        ailink=FakeClient(usd="3000.00"),
        callback_http=http,
        now=utcnow(),
    )

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

    await run_once(
        session_factory=session_factory,
        settings=make_settings(),
        byoc=None,
        ailink=FakeClient(usd="3000.00", deliver_immediately=False),
        callback_http=http,
        now=utcnow(),
    )

    db_session.refresh(task)
    assert task.status == "processing"
    assert task.submit_attempts == 1
    assert "等待 webhook" in (task.last_error or "")
    assert task.next_run_at > utcnow()
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
    now = utcnow()

    await run_once(
        session_factory=session_factory,
        settings=settings,
        byoc=None,
        ailink=client,
        callback_http=http,
        now=now,
    )
    db_session.refresh(task)
    assert task.status == "processing"
    assert task.submit_attempts == 1
    assert "开卡被平台拒绝" in (task.last_error or "")

    await run_once(
        session_factory=session_factory,
        settings=settings,
        byoc=None,
        ailink=client,
        callback_http=http,
        now=now + timedelta(seconds=121),
    )
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
    now = utcnow()

    for offset in (0, 121, 242):
        await run_once(
            session_factory=session_factory,
            settings=settings,
            byoc=None,
            ailink=client,
            callback_http=http,
            now=now + timedelta(seconds=offset),
        )

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
    now = utcnow()

    await run_once(
        session_factory=session_factory,
        settings=settings,
        byoc=None,
        ailink=client,
        callback_http=http,
        now=now,
    )
    db_session.refresh(task)
    assert task.status == "processing"
    assert "BIN" in (task.last_error or "")

    await run_once(
        session_factory=session_factory,
        settings=settings,
        byoc=None,
        ailink=client,
        callback_http=http,
        now=now + timedelta(seconds=121),
    )
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

    await run_once(
        session_factory=session_factory,
        settings=make_settings(open_card_max_attempts=1),
        byoc=None,
        ailink=FakeClient(usd="3000.00", quota_remaining=0),
        callback_http=http,
        now=utcnow(),
    )

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

    await run_once(
        session_factory=session_factory,
        settings=make_settings(open_card_max_attempts=1),
        byoc=None,
        ailink=FakeClient(usd="3000.00", initial_funding_status="FAILED"),
        callback_http=http,
        now=utcnow(),
    )

    db_session.refresh(task)
    assert task.status == "failed"
    assert "首充失败" in (task.last_error or "")
    await http.aclose()


async def test_run_once_skips_tasks_not_due(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, _ = callback_client()

    await run_once(
        session_factory=session_factory,
        settings=make_settings(),
        byoc=FakeByoc(),
        ailink=None,
        callback_http=http,
        now=utcnow() - timedelta(hours=1),
    )

    db_session.refresh(task)
    assert task.status == "pending"
    await http.aclose()


async def test_run_once_retries_failed_callback(db_session, session_factory):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, _ = callback_client(status_code=500)

    await run_once(
        session_factory=session_factory,
        settings=make_settings(subscription_callback_max_attempts=3),
        byoc=FakeByoc(),
        ailink=None,
        callback_http=http,
        now=utcnow(),
    )

    db_session.refresh(task)
    assert task.status == "succeeded"
    assert task.callback_delivered is False
    assert task.callback_attempts == 1
    assert task.callback_next_at is not None
    await http.aclose()


def test_stale_running_task_can_be_reclaimed(db_session):
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121200000001",
    )
    now = utcnow()
    task.status = "running"
    task.claimed_until = now - timedelta(seconds=1)
    task.next_run_at = now - timedelta(seconds=1)
    crud.save_task(db_session, task)

    claimed = crud.claim_next_task(
        db_session,
        now=now,
        claim_until=now + timedelta(minutes=5),
    )

    assert claimed is not None
    assert claimed.id == task.id


def test_running_task_with_active_claim_is_not_reclaimed(db_session):
    task = crud.create_task(
        db_session,
        kind="open_card",
        plan="plus",
        request_id="order-202611121200000002",
    )
    now = utcnow()
    task.status = "running"
    task.claimed_until = now + timedelta(minutes=5)
    task.next_run_at = now - timedelta(seconds=1)
    crud.save_task(db_session, task)

    assert crud.claim_next_task(db_session, now=now, claim_until=now + timedelta(minutes=5)) is None


async def test_run_once_logs_task_start_and_finish(db_session, session_factory, caplog):
    make_card(db_session, plan="plus")
    task = new_task(db_session)
    http, _ = callback_client()

    with caplog.at_level(logging.INFO, logger="app.jobs.task_worker"):
        await run_once(
            session_factory=session_factory,
            settings=make_settings(),
            byoc=FakeByoc(),
            ailink=None,
            callback_http=http,
            now=utcnow(),
        )

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
    now = utcnow()
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
    running.claimed_until = now + timedelta(minutes=5)
    crud.save_task(db_session, running)

    counts = crud.task_status_counts(db_session)
    assert counts.get("pending", 0) == before.get("pending", 0) + 1
    assert counts.get("running", 0) == before.get("running", 0) + 1

    ids = {task.id for task in crud.list_running_tasks(db_session, now=now)}
    assert running.id in ids
    assert pending.id not in ids
