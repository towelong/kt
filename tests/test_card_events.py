"""开卡/交易事件落库：三要素、计次、余额、幂等。"""

import logging
from decimal import Decimal

from sqlmodel import select

from app import crud
from app.models import CardOperation
from app.services.card_events import (
    handle_card_issue,
    handle_card_operation,
    handle_card_transaction,
)
from tests.factories import (
    CARD_NUMBER,
    REQUEST_NO,
    WEBHOOK_SECRET,
    issue_event,
    make_card,
    operation_event,
    transaction_event,
)


def test_issue_success_fills_secrets_and_enables(db_session, make_body, encrypt_field):
    card = crud.create_card_intent(
        db_session,
        plan="plus",
        request_no=REQUEST_NO,
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
    # 开卡本身不记余额，首充成功前也不启用
    assert card.enabled is False
    assert card.balance == Decimal("0.00")
    assert card.applied_at is not None

    handle_card_operation(
        db_session,
        operation_event(
            make_body,
            amount="48.00",
            event_id="evt_initial_funding",
            funding_purpose="INITIAL_FUNDING",
            initial_funding_status="SUCCESS",
            card_issue_request_no=REQUEST_NO,
        ),
    )

    db_session.refresh(card)
    assert card.balance == Decimal("48.00")
    assert card.initial_funding_status == "SUCCESS"
    assert card.enabled is True


def test_initial_funding_before_issue_credits_placeholder(db_session, make_body, encrypt_field):
    """首充比开卡 webhook 先到：用 cardIssueRequestNo 认领占位行，余额不丢。"""
    card = crud.create_card_intent(db_session, plan="plus", request_no=REQUEST_NO)

    handle_card_operation(
        db_session,
        operation_event(
            make_body,
            amount="48.00",
            event_id="evt_funding_first",
            funding_purpose="INITIAL_FUNDING",
            initial_funding_status="SUCCESS",
            card_issue_request_no=REQUEST_NO,
        ),
    )

    db_session.refresh(card)
    assert card.card_id == "VC1"
    assert card.balance == Decimal("48.00")
    assert card.initial_funding_status == "SUCCESS"
    assert card.enabled is False

    handle_card_issue(
        db_session,
        issue_event(make_body, encrypt_field),
        webhook_secret=WEBHOOK_SECRET,
    )

    db_session.refresh(card)
    assert card.balance == Decimal("48.00")
    assert card.initial_funding_status == "SUCCESS"
    assert card.enabled is True


def test_issue_creates_row_when_placeholder_missing(db_session, make_body, encrypt_field):
    event = issue_event(
        make_body,
        encrypt_field,
        request_no="kaika_pro500_1730000000000cd34",
        card_id="VC9",
        initial_funding="SUCCESS",
    )

    handle_card_issue(db_session, event, webhook_secret=WEBHOOK_SECRET)

    card = crud.get_card_by_platform_id(db_session, "VC9")
    assert card is not None
    assert card.plan == "pro500"
    assert card.enabled is True
    assert card.balance == Decimal("0.00")


def test_issue_without_webhook_secret_stays_disabled(db_session, make_body, encrypt_field):
    card = crud.create_card_intent(
        db_session,
        plan="plus",
        request_no=REQUEST_NO,
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
    )

    handle_card_issue(
        db_session,
        issue_event(make_body, encrypt_field, event_type="CARD_ISSUE.FAILED"),
        webhook_secret=WEBHOOK_SECRET,
    )

    db_session.refresh(card)
    assert card.apply_status == "FAILED"
    assert card.enabled is False


def test_auth_success_first_deducts_once(db_session, make_body):
    card = make_card(db_session)

    handle_card_transaction(db_session, transaction_event(make_body))
    handle_card_transaction(
        db_session,
        transaction_event(make_body, event_id="evt_tx_1_retry"),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("36.13")
    transaction = crud.get_card_transaction(db_session, "txn_1")
    assert transaction is not None
    assert transaction.use_counted is True
    assert transaction.balance_applied is True
    assert transaction.applied_amount == Decimal("36.13")
    assert transaction.event_status == "AUTH_SUCCESS"


async def test_settled_first_deducts_once(db_session, make_body):
    card = make_card(db_session)

    handle_card_transaction(db_session, transaction_event(make_body, status="SETTLED"))
    handle_card_transaction(
        db_session,
        transaction_event(make_body, status="SETTLED", event_id="evt_tx_2"),
    )

    db_session.refresh(card)
    assert card.balance == Decimal("48.00") - Decimal("36.13")
    assert card.used_count == 1
    transaction = crud.get_card_transaction(db_session, "txn_1")
    assert transaction is not None
    assert transaction.balance_applied is True
    assert transaction.applied_amount == Decimal("36.13")


def test_refund_decrements_count_and_returns_balance(db_session, make_body):
    card = make_card(db_session, used_count=2, balance=Decimal("20.00"))

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_1",
            status="AUTH_SUCCESS",
            actual="10.00",
            event_id="evt_charge",
        ),
    )
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
    assert card.used_count == 2
    assert card.balance == Decimal("20.00")
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
        transaction_event(
            make_body,
            transaction_id="txn_3",
            status="PROCESSING",
            actual="1.00",
            event_id="evt_3",
        ),
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
    assert parse_plan_from_request_no("manual_card") == "plus"
    assert parse_plan_from_request_no(None) == "plus"


async def test_processing_deducts_balance_and_counts_use(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_processing",
            status="PROCESSING",
            actual="15.63",
            original="982.14",
            event_id="evt_processing",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")
    assert card.enabled is True
    transaction = crud.get_card_transaction(db_session, "txn_processing")
    assert transaction is not None
    assert transaction.use_counted is True
    assert transaction.balance_applied is True


async def test_processing_then_auth_success_does_not_double(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_stage",
            status="PROCESSING",
            actual="15.63",
            event_id="evt_stage_1",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_stage",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_stage_2",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")


async def test_processing_then_settled_keeps_precharged_amount(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settle",
            status="PROCESSING",
            actual="15.63",
            event_id="evt_settle_1",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settle",
            status="SETTLED",
            actual="16.00",
            event_id="evt_settle_2",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    # 以 PROCESSING 的预扣金额为准，SETTLED 不再补差
    assert card.balance == Decimal("48.00") - Decimal("15.63")


async def test_processing_then_auth_failed_refunds_and_releases_use(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_fail",
            status="PROCESSING",
            actual="15.63",
            event_id="evt_fail_1",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_fail",
            status="AUTH_FAILED",
            actual="0.00",
            event_id="evt_fail_2",
        ),
    )
    # 失败事件重投不能把已退回的钱再加一次
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_fail",
            status="AUTH_FAILED",
            actual="0.00",
            event_id="evt_fail_3",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00")
    assert card.enabled is True


async def test_processing_retry_does_not_double(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    for event_id in ("evt_retry_1", "evt_retry_2"):
        handle_card_transaction(
            db_session,
            transaction_event(
                make_body,
                transaction_id="txn_retry",
                status="PROCESSING",
                actual="15.63",
                event_id=event_id,
            ),
        )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")


async def test_auth_success_then_processing_keeps_first_amount(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_reverse",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_reverse_1",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_reverse",
            status="PROCESSING",
            actual="16.00",
            event_id="evt_reverse_2",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")
    transaction = crud.get_card_transaction(db_session, "txn_reverse")
    assert transaction is not None
    assert transaction.event_status == "AUTH_SUCCESS"


async def test_settled_then_processing_does_not_regress_status(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_rank",
            status="SETTLED",
            actual="15.63",
            event_id="evt_rank_1",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_rank",
            status="PROCESSING",
            actual="15.63",
            event_id="evt_rank_2",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")
    transaction = crud.get_card_transaction(db_session, "txn_rank")
    assert transaction is not None
    assert transaction.event_status == "SETTLED"


async def test_settled_with_new_id_related_to_charge_does_not_deduct(db_session, make_body):
    """SETTLED 换新 transactionId 且 related 指回已扣款的原单时，只推进状态、不再扣钱。"""
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_auth",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_auth",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="SETTLED",
            actual="15.63",
            related="txn_auth",
            event_id="evt_settled",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")
    settled = crud.get_card_transaction(db_session, "txn_settled")
    assert settled is not None
    assert settled.event_status == "SETTLED"
    assert settled.balance_applied is True
    assert settled.applied_amount == Decimal("0.00")
    assert settled.use_counted is True


async def test_settled_related_to_processing_does_not_deduct(db_session, make_body):
    """PROCESSING 直接到 SETTLED、SETTLED 换新 ID 指回 PROCESSING 时，也只扣一次。"""
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_processing",
            status="PROCESSING",
            actual="15.63",
            event_id="evt_processing",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="SETTLED",
            actual="15.63",
            related="txn_processing",
            event_id="evt_settled",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")
    settled = crud.get_card_transaction(db_session, "txn_settled")
    assert settled is not None
    assert settled.event_status == "SETTLED"
    assert settled.balance_applied is True
    assert settled.applied_amount == Decimal("0.00")


async def test_auth_failed_on_settled_related_to_processing_returns_balance(db_session, make_body):
    """AUTH_FAILED 打在关联 PROCESSING 的 SETTLED 行上，退回 PROCESSING 扣的钱。"""
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_processing",
            status="PROCESSING",
            actual="15.63",
            event_id="evt_processing",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="SETTLED",
            actual="15.63",
            related="txn_processing",
            event_id="evt_settled",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="AUTH_FAILED",
            actual="0.00",
            related="txn_processing",
            event_id="evt_failed",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00")
    processing = crud.get_card_transaction(db_session, "txn_processing")
    assert processing is not None
    assert processing.balance_applied is False
    assert processing.applied_amount == Decimal("0.00")


async def test_settled_before_original_transaction_does_not_double(db_session, make_body):
    """SETTLED 先到、原单后到：反向关联也认，整笔只扣一次。"""
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="SETTLED",
            actual="15.63",
            related="txn_auth",
            event_id="evt_settled",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_auth",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_auth",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")
    auth = crud.get_card_transaction(db_session, "txn_auth")
    assert auth is not None
    assert auth.balance_applied is True
    assert auth.applied_amount == Decimal("0.00")


async def test_auth_failed_on_related_transaction_returns_balance(db_session, make_body):
    """AUTH_FAILED 打在关联行上，也要退回钱和次数，不能只看本行 applied_amount。"""
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_auth",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_auth",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="SETTLED",
            actual="15.63",
            related="txn_auth",
            event_id="evt_settled",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="AUTH_FAILED",
            actual="0.00",
            related="txn_auth",
            event_id="evt_failed",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00")
    auth = crud.get_card_transaction(db_session, "txn_auth")
    assert auth is not None
    assert auth.balance_applied is False
    assert auth.applied_amount == Decimal("0.00")


async def test_refund_against_settled_related_transaction_credits(db_session, make_body):
    """退款关联到 SETTLED 的关联行时，仍要按链上扣款行退钱并减次数。"""
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_auth",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_auth",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="SETTLED",
            actual="15.63",
            related="txn_auth",
            event_id="evt_settled",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_refund",
            status="REFUND_SUCCESS",
            actual="5.00",
            related="txn_settled",
            event_id="evt_refund",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00") - Decimal("15.63") + Decimal("5.00")
    refund = crud.get_card_transaction(db_session, "txn_refund")
    assert refund is not None
    assert refund.balance_applied is True


async def test_refund_waiting_for_original_applies_when_charge_lands(db_session, make_body):
    """退款先挂起并指向原单，扣款落在 SETTLED 新行上时，原单到达后补记退款。"""
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_refund",
            status="REFUND_SUCCESS",
            actual="5.00",
            related="txn_auth",
            event_id="evt_refund",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_settled",
            status="SETTLED",
            actual="15.63",
            related="txn_auth",
            event_id="evt_settled",
        ),
    )
    db_session.refresh(card)
    assert card.balance == Decimal("48.00") - Decimal("15.63")

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_auth",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_auth",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00") - Decimal("15.63") + Decimal("5.00")
    refund = crud.get_card_transaction(db_session, "txn_refund")
    assert refund is not None
    assert refund.balance_applied is True


async def test_correction_with_related_transaction_still_deducts(db_session, make_body):
    """CORRECTION 带 related 是补扣差额，不能被链上去重跳过。"""
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_auth",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_auth",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_correction",
            status="CORRECTION",
            actual="2.00",
            related="txn_auth",
            event_id="evt_correction",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63") - Decimal("2.00")


async def test_zero_amount_processing_waits_for_auth_success(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_zero",
            status="PROCESSING",
            actual="0.00",
            event_id="evt_zero_1",
        ),
    )
    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00")

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_zero",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_zero_2",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 1
    assert card.balance == Decimal("48.00") - Decimal("15.63")


async def test_auth_failed_first_blocks_later_charge(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_dead",
            status="AUTH_FAILED",
            actual="0.00",
            event_id="evt_dead_1",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_dead",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_dead_2",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00")
    transaction = crud.get_card_transaction(db_session, "txn_dead")
    assert transaction is not None
    assert transaction.event_status == "AUTH_FAILED"
    assert transaction.balance_applied is False


async def test_auth_failed_refunds_applied_amount_not_latest_actual(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_revert",
            status="PROCESSING",
            actual="15.63",
            event_id="evt_revert_1",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_revert",
            status="AUTH_SUCCESS",
            actual="16.00",
            event_id="evt_revert_2",
        ),
    )
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_revert",
            status="AUTH_FAILED",
            actual="0.00",
            event_id="evt_revert_3",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00")
    transaction = crud.get_card_transaction(db_session, "txn_revert")
    assert transaction is not None
    assert transaction.applied_amount == Decimal("0.00")
    assert transaction.balance_applied is False


async def test_refund_before_charge_applies_when_charge_arrives(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_refund_early",
            status="REFUND_SUCCESS",
            actual="10.00",
            related="txn_ordered",
            event_id="evt_refund_early",
        ),
    )
    db_session.refresh(card)
    assert card.balance == Decimal("48.00")
    assert card.used_count == 0

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_ordered",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_ordered",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00") - Decimal("15.63") + Decimal("10.00")
    refund = crud.get_card_transaction(db_session, "txn_refund_early")
    assert refund is not None
    assert refund.balance_applied is True
    assert refund.applied_amount == Decimal("10.00")


async def test_refund_duplicate_does_not_credit_twice(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_dup_charge",
            status="AUTH_SUCCESS",
            actual="15.63",
            event_id="evt_dup_charge",
        ),
    )
    for event_id in ("evt_dup_refund_1", "evt_dup_refund_2"):
        handle_card_transaction(
            db_session,
            transaction_event(
                make_body,
                transaction_id="txn_dup_refund",
                status="REFUND_SUCCESS",
                actual="5.00",
                related="txn_dup_charge",
                event_id=event_id,
            ),
        )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00") - Decimal("15.63") + Decimal("5.00")


async def test_refund_without_applied_original_does_not_credit(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))
    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_orphan_refund",
            status="REFUND_SUCCESS",
            actual="10.00",
            related="txn_missing",
            event_id="evt_orphan_refund",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00")
    refund = crud.get_card_transaction(db_session, "txn_orphan_refund")
    assert refund is not None
    assert refund.balance_applied is False


async def test_transaction_currencies_are_stored(db_session, make_body):
    make_card(db_session, balance=Decimal("100.00"))

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_currency",
            status="PROCESSING",
            actual="15.63",
            original="982.14",
            original_currency="PHP",
            actual_currency="USD",
            event_id="evt_currency",
        ),
    )

    transaction = crud.get_card_transaction(db_session, "txn_currency")
    assert transaction is not None
    assert transaction.original_currency == "PHP"
    assert transaction.actual_currency == "USD"
    assert transaction.original_amount == Decimal("982.14")
    assert transaction.actual_amount == Decimal("15.63")


async def test_auth_failed_stores_default_remark(db_session, make_body):
    make_card(db_session, balance=Decimal("48.00"))

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_failed_remark",
            status="AUTH_FAILED",
            actual="0.00",
            event_id="evt_failed_remark",
        ),
    )

    transaction = crud.get_card_transaction(db_session, "txn_failed_remark")
    assert transaction is not None
    assert transaction.remark == "授权失败"


async def test_transaction_platform_remark_is_stored(db_session, make_body):
    make_card(db_session, balance=Decimal("48.00"))

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_platform_remark",
            status="AUTH_FAILED",
            actual="0.00",
            remark="商户拒绝：余额不足",
            event_id="evt_platform_remark",
        ),
    )

    transaction = crud.get_card_transaction(db_session, "txn_platform_remark")
    assert transaction is not None
    assert transaction.remark == "商户拒绝：余额不足"


async def test_correction_deducts_without_counting_use(db_session, make_body):
    card = make_card(db_session, balance=Decimal("48.00"))

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_correction",
            status="CORRECTION",
            actual="1.20",
            original="1.20",
            event_id="evt_correction",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 0
    assert card.balance == Decimal("48.00") - Decimal("1.20")


def test_auth_failed_without_charge_does_not_decrement(db_session, make_body):
    card = make_card(db_session, used_count=2)

    handle_card_transaction(
        db_session,
        transaction_event(
            make_body,
            transaction_id="txn_failed_once",
            status="AUTH_FAILED",
            actual="0.00",
            event_id="evt_failed_once",
        ),
    )

    db_session.refresh(card)
    assert card.used_count == 2
    assert card.balance == Decimal("48.00")


def test_recharge_success_adds_balance_and_is_idempotent(db_session, make_body):
    card = make_card(db_session, balance=Decimal("0.00"))
    event = operation_event(make_body, amount="16.00", event_id="evt_recharge_1")

    handle_card_operation(db_session, event)
    handle_card_operation(db_session, event)

    db_session.refresh(card)
    assert card.balance == Decimal("16.00")
    operations = db_session.exec(select(CardOperation)).all()
    assert len(operations) == 1
    assert operations[0].event_id == "evt_recharge_1"
    assert operations[0].amount == Decimal("16.00")


def test_withdraw_success_subtracts_balance(db_session, make_body):
    card = make_card(db_session, balance=Decimal("20.00"))

    handle_card_operation(
        db_session,
        operation_event(
            make_body,
            event_type="CARD_OPERATION.WITHDRAW_SUCCESS",
            amount="5.00",
            event_id="evt_withdraw_1",
        ),
    )

    db_session.refresh(card)
    assert card.balance == Decimal("15.00")


def test_initial_funding_recharge_updates_status(db_session, make_body):
    card = make_card(db_session, balance=Decimal("0.00"))

    handle_card_operation(
        db_session,
        operation_event(
            make_body,
            amount="16.00",
            event_id="evt_initial_funding",
            funding_purpose="INITIAL_FUNDING",
            initial_funding_status="SUCCESS",
            card_issue_request_no=REQUEST_NO,
        ),
    )

    db_session.refresh(card)
    assert card.balance == Decimal("16.00")
    assert card.initial_funding_status == "SUCCESS"
