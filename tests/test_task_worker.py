"""任务 worker：认领、订阅执行、补卡任务、回调重试。"""

from datetime import timedelta

import httpx
from sqlmodel import select

from app import crud
from app.jobs.task_worker import run_once
from app.models import Card, utcnow
from tests.factories import FakeClient, make_card
from tests.test_subscriptions import FakeByoc, make_settings, new_task, order


def callback_client(status_code=200):
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "body": request.content})
        return httpx.Response(status_code, text="ok")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), captured


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


async def test_run_once_without_byoc_waits(db_session, session_factory):
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
    assert task.status == "waiting_card"
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
