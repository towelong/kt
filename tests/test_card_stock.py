"""卡库存策略：缺口计算、余额/兑换、BIN 选择、租约、跨层联动。"""

import logging
from datetime import timedelta
from decimal import Decimal

from sqlmodel import select

from app import crud
from app.models import Card, utcnow
from app.services.card_events import handle_card_issue, handle_card_operation
from app.services.card_stock import PLAN_SPECS, replenish
from tests.factories import (
    REQUEST_NO,
    WEBHOOK_SECRET,
    FakeClient,
    add_usable_card,
    cover_all_plans,
    issue_event,
    operation_event,
    us_bin,
)


async def test_no_missing_plans_makes_no_api_calls(db_session):
    cover_all_plans(db_session)
    client = FakeClient(usd="3000.00")

    report = await replenish(db_session, client, now=utcnow())

    assert report.missing == []
    assert client.calls == []


async def test_usd_is_enough_issues_one_card_per_plan(db_session):
    client = FakeClient(usd="3000.00")

    report = await replenish(db_session, client, now=utcnow())

    assert report.applied == ["plus", "pro100", "pro200", "pro500"]
    assert report.failed == []
    assert client.conversions == []
    assert [request.recharge_amount for request in client.applies] == [
        Decimal("48.00"),
        Decimal("270.00"),
        Decimal("450.00"),
        Decimal("1410.00"),
    ]
    assert all(request.card_type == "PREPAID" for request in client.applies)
    assert all("kaika_" in request.request_no for request in client.applies)
    cards = db_session.exec(select(Card).order_by(Card.id)).all()
    assert [card.plan for card in cards] == ["plus", "pro100", "pro200", "pro500"]
    # 三要素要等 webhook，先不启用
    assert all(card.apply_status == "SUCCESS" and not card.enabled for card in cards)
    assert all(card.card_id for card in cards)


async def test_short_usd_converts_usdt_then_issues(db_session):
    client = FakeClient(usd="10.00", usdt="3000.00")

    report = await replenish(db_session, client, now=utcnow())

    assert client.conversions == [Decimal("2174.53")]
    assert report.converted_usdt == Decimal("2174.53")
    assert report.applied == ["plus", "pro100", "pro200", "pro500"]
    cards = db_session.exec(select(Card)).all()
    assert len(cards) == 4


async def test_insufficient_balance_logs_error_and_skips(db_session, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.card_stock")
    client = FakeClient(usd="1.00", usdt="1.00")

    report = await replenish(db_session, client, now=utcnow())

    assert "余额不足" in caplog.text
    assert client.applies == []
    assert report.applied == []
    assert db_session.exec(select(Card)).all() == []


async def test_recent_incomplete_card_blocks_duplicate_issue(db_session, caplog):
    caplog.set_level(logging.WARNING, logger="app.services.card_stock")
    for spec in PLAN_SPECS:
        if spec.name != "plus":
            add_usable_card(db_session, spec.name)
    crud.create_card_intent(
        db_session,
        plan="plus",
        request_no="kaika_plus_recent0001",
    )
    client = FakeClient(usd="3000.00")

    report = await replenish(db_session, client, now=utcnow())

    assert report.missing == []
    assert client.calls == []
    assert "未完成的开卡申请" in caplog.text


async def test_without_us_bin_logs_error(db_session, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.card_stock")
    client = FakeClient(usd="3000.00", bins=[us_bin(country="SG")])

    report = await replenish(db_session, client, now=utcnow())

    assert "没有可用的美国卡 BIN" in caplog.text
    assert client.applies == []
    assert report.failed == ["plus", "pro100", "pro200", "pro500"]


async def test_us_bin_is_used_regardless_of_min_amount(db_session, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.card_stock")
    client = FakeClient(usd="3000.00", bins=[us_bin(min_open="5000.00")])

    report = await replenish(db_session, client, now=utcnow())

    assert report.applied == ["plus", "pro100", "pro200", "pro500"]
    assert client.applies[0].card_bin_id == 1001


async def test_non_us_bin_is_skipped(db_session):
    client = FakeClient(
        usd="3000.00",
        bins=[us_bin(bin_id=1, country="SG"), us_bin(bin_id=2, country="US")],
    )

    await replenish(db_session, client, now=utcnow())

    assert all(request.card_bin_id == 2 for request in client.applies)


async def test_quota_exhausted_logs_error(db_session, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.card_stock")
    client = FakeClient(usd="3000.00", quota_remaining=0)

    report = await replenish(db_session, client, now=utcnow())

    assert "配额已用尽" in caplog.text
    assert client.applies == []
    assert report.missing  # 缺口是知道的，只是没法开


async def test_business_rejection_marks_card_failed(db_session, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.card_stock")

    class RejectingClient(FakeClient):
        async def apply_cards(self, request, *, request_id=None):
            from app.integrations.ailink.errors import AiLinkBusinessError

            self.calls.append("apply")
            raise AiLinkBusinessError("余额不足，开卡失败", status_code=400, request_id="req1")

    client = RejectingClient(usd="3000.00")

    report = await replenish(db_session, client, now=utcnow())

    assert "余额不足，开卡失败" in caplog.text
    assert report.failed == ["plus", "pro100", "pro200", "pro500"]
    cards = db_session.exec(select(Card)).all()
    assert len(cards) == 4
    assert all(card.apply_status == "FAILED" and not card.enabled for card in cards)


def test_job_lease_is_exclusive(db_session):
    crud.ensure_job_lease(db_session, "card_stock")

    assert (
        crud.acquire_job_lease(
            db_session,
            "card_stock",
            locked_until=utcnow() + timedelta(minutes=5),
        )
        is True
    )
    assert (
        crud.acquire_job_lease(
            db_session,
            "card_stock",
            locked_until=utcnow() + timedelta(minutes=5),
        )
        is False
    )

    crud.release_job_lease(db_session, "card_stock")
    assert (
        crud.acquire_job_lease(
            db_session,
            "card_stock",
            locked_until=utcnow() + timedelta(minutes=5),
        )
        is True
    )


async def test_webhook_issued_card_is_usable(db_session, make_body, encrypt_field):
    """开卡 webhook 补齐三要素后，扫描不再为这个套餐补卡。"""
    for spec in PLAN_SPECS:
        if spec.name != "plus":
            add_usable_card(db_session, spec.name)
    crud.create_card_intent(
        db_session,
        plan="plus",
        request_no=REQUEST_NO,
    )
    handle_card_issue(
        db_session,
        issue_event(make_body, encrypt_field),
        webhook_secret=WEBHOOK_SECRET,
    )
    handle_card_operation(
        db_session,
        operation_event(
            make_body,
            amount="48.00",
            event_id="evt_funding_plus",
            funding_purpose="INITIAL_FUNDING",
            initial_funding_status="SUCCESS",
            card_issue_request_no=REQUEST_NO,
        ),
    )

    client = FakeClient(usd="3000.00")
    report = await replenish(db_session, client, now=utcnow())

    assert report.missing == []
    assert client.calls == []


async def test_used_up_card_is_replaced(db_session):
    """次数用完停用的卡不算“进行中”，下一轮直接补一张新的。"""
    used_up = add_usable_card(db_session, "plus")
    used_up.used_count = 3
    used_up.enabled = False
    crud.save_card(db_session, used_up)
    for spec in PLAN_SPECS:
        if spec.name != "plus":
            add_usable_card(db_session, spec.name)

    client = FakeClient(usd="3000.00")
    report = await replenish(db_session, client, now=utcnow())

    assert report.missing == ["plus"]
    assert report.applied == ["plus"]
    assert len(client.applies) == 1


async def test_us_bin_must_start_with_5378(db_session, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.card_stock")
    client = FakeClient(usd="3000.00", bins=[us_bin(bin_number="486880")])

    report = await replenish(db_session, client, now=utcnow())

    assert client.applies == []
    assert report.applied == []
    assert "没有可用的美国卡 BIN" in caplog.text


async def test_5378_bin_must_be_us(db_session, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.card_stock")
    client = FakeClient(
        usd="3000.00",
        bins=[us_bin(bin_number="537800", country="SG")],
    )

    await replenish(db_session, client, now=utcnow())

    assert client.applies == []
    assert "没有可用的美国卡 BIN" in caplog.text


async def test_5378_us_bin_is_selected(db_session):
    client = FakeClient(
        usd="3000.00",
        bins=[
            us_bin(bin_id=1, bin_number="486880"),
            us_bin(bin_id=2, bin_number="537800"),
        ],
    )

    await replenish(db_session, client, now=utcnow())

    assert client.applies
    assert all(request.card_bin_id == 2 for request in client.applies)


async def test_low_balance_card_triggers_new_card(db_session):
    card = add_usable_card(db_session, "plus")
    card.balance = Decimal("1.00")
    crud.save_card(db_session, card)
    client = FakeClient(usd="3000.00")

    report = await replenish(db_session, client, now=utcnow())

    assert "plus" in report.applied
    assert len(client.applies) >= 1
