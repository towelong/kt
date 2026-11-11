"""开卡/交易事件落库：三要素、计次、余额、幂等。"""

import logging
from decimal import Decimal

from app import crud
from app.services.card_events import handle_card_issue, handle_card_transaction
from tests.factories import (
    CARD_NUMBER,
    REQUEST_NO,
    WEBHOOK_SECRET,
    issue_event,
    make_card,
    transaction_event,
)


def test_issue_success_fills_secrets_and_enables(db_session, make_body, encrypt_field):
    card = crud.create_card_intent(
        db_session,
        plan="plus",
        request_no=REQUEST_NO,
        recharge_amount=Decimal("48.00"),
    )
    event = issue_event(make_body, encrypt_field)

    handle_card_issue(db_session, event, webhook_secret=WEBHOOK_SECRET)

    db_session.refresh(card)
    assert card.card_id == "VC1"
    assert card.card_no == CARD_NUMBER
    assert card.expiry == "12/29"
    assert card.cvv == "123"
    assert card.apply_status == "SUCCESS"
    assert card.initial_funding_status == "PENDING"
    assert card.enabled is True
    assert card.balance == Decimal("48.00")
    assert card.applied_at is not None


def test_issue_creates_row_when_placeholder_missing(db_session, make_body, encrypt_field):
    event = issue_event(
        make_body,
        encrypt_field,
        request_no="kaika_pro500_1730000000000cd34",
        card_id="VC9",
    )

    handle_card_issue(db_session, event, webhook_secret=WEBHOOK_SECRET)

    card = crud.get_card_by_platform_id(db_session, "VC9")
    assert card is not None
    assert card.plan == "pro500"
    assert card.enabled is True


def test_issue_without_webhook_secret_stays_disabled(db_session, make_body, encrypt_field):
    card = crud.create_card_intent(
        db_session,
        plan="plus",
        request_no=REQUEST_NO,
        recharge_amount=Decimal("48.00"),
    )

    handle_card_issue(db_session, issue_event(make_body, encrypt_field), webhook_secret=None)

    db_session.refresh(card)
    assert card.card_no is None
    assert card.enabled is False


def test_issue_failed_marks_card_failed(db_session, make_body, encrypt_field):
    card = crud.create_card_intent(
        db_session,
        plan="plus",
        request_no=REQUEST_NO,
        recharge_amount=Decimal("48.00"),
    )

    handle_card_issue(
        db_session,
        issue_event(make_body, encrypt_field, event_type="CARD_ISSUE.FAILED"),
        webhook_secret=WEBHOOK_SECRET,
    )

    db_session.refresh(card)
    assert card.apply_status == "FAILED"
    assert card.enabled is False


def test_auth_success_counts_once_per_transaction(db_session, make_body):
    card = make_card(db_session)

    handle_card_transaction(db_session, transaction_event(make_body))
    handle_card_transaction(
        db_session,
        transaction_event(make_body, event_id="evt_tx_1_retry"),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00")
    transaction = crud.get_card_transaction(db_session, "txn_1")
    assert transaction is not None
    assert transaction.use_counted is True
    assert transaction.balance_applied is False
    assert transaction.event_status == "AUTH_SUCCESS"


def test_settled_deducts_balance_once(db_session, make_body):
    card = make_card(db_session)

    handle_card_transaction(db_session, transaction_event(make_body, status="SETTLED"))
    handle_card_transaction(
        db_session,
        transaction_event(make_body, status="SETTLED", event_id="evt_tx_2"),
    )

    db_session.refresh(card)
    assert card.balance == Decimal("48.00") - Decimal("36.13")
    assert card.used_count == 0
    transaction = crud.get_card_transaction(db_session, "txn_1")
    assert transaction is not None
    assert transaction.balance_applied is True


def test_refund_decrements_count_and_returns_balance(db_session, make_body):
    card = make_card(db_session, used_count=2, balance=Decimal("20.00"))

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_refund",
            status="REFUND_SUCCESS",
            actual="10.00",
            related="txn_1",
            event_id="evt_refund",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("30.00")
    assert card.enabled is True


def test_auth_failed_never_goes_negative(db_session, make_body):
    card = make_card(db_session, used_count=0)

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_failed",
            status="AUTH_FAILED",
            actual="0.00",
            event_id="evt_failed",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0


def test_third_use_disables_card(db_session, make_body):
    card = make_card(db_session, used_count=2)

    handle_card_transaction(
        db_session,
        transaction_event(make_body, transaction_id="txn_3", event_id="evt_3"),
    )

    db_session.refresh(card)
    assert card.used_count == 3
    assert card.enabled is False


def test_unknown_card_transaction_logs_error(db_session, make_body, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.card_events")

    handle_card_transaction(
        db_session,
        transaction_event(make_body, card_id="VC_UNKNOWN", event_id="evt_unknown"),
    )

    assert "找不到本地卡" in caplog.text
    transaction = crud.get_card_transaction(db_session, "txn_1")
    assert transaction is not None
    assert transaction.use_counted is False


def test_parse_plan_from_request_no():
    from app.services.card_events import parse_plan_from_request_no

    assert parse_plan_from_request_no(REQUEST_NO) == "plus"
    assert parse_plan_from_request_no("kaika_pro100_1730000000000ab12") == "pro100"
    assert parse_plan_from_request_no("manual_card") == "unknown"
    assert parse_plan_from_request_no(None) == "unknown"
