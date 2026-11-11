"""Webhook 事件落库：开卡补三要素、交易计数与余额。

计数口径（每笔 transaction 只落一次）：
- AUTH_SUCCESS +1；AUTH_FAILED -1；REFUND_SUCCESS / REVERSE_SUCCESS -1；不低于 0。
- SETTLED 扣 actual_amount；退款/撤销加回 actual_amount。
- 次数到 3 停用，退款把次数降回来会重新启用。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlmodel import Session

from app import crud
from app.integrations.ailink.errors import AiLinkDecryptError
from app.integrations.ailink.events import CardIssueEvent, CardTransactionEvent, IssuedCard
from app.models import Card, CardPlan, CardTransaction, utcnow

logger = logging.getLogger(__name__)

MAX_USES = 3

# 每笔交易对使用次数的增量；不在表里的状态不动次数
COUNT_DELTA: dict[str, int] = {
    "AUTH_SUCCESS": 1,
    "AUTH_FAILED": -1,
    "REFUND_SUCCESS": -1,
    "REVERSE_SUCCESS": -1,
}

# 每笔交易对卡余额的增量（actual_amount 已含手续费，只加一次）
BALANCE_DELTA: dict[str, int] = {
    "SETTLED": -1,
    "REFUND_SUCCESS": 1,
    "REVERSE_SUCCESS": 1,
}

_KNOWN_PLANS = {plan.value for plan in CardPlan}


def parse_plan_from_request_no(request_no: str | None) -> str:
    """requestNo 形如 kaika_plus_1730000000000ab12，开卡 webhook 用它回填套餐。"""
    for part in (request_no or "").split("_"):
        if part in _KNOWN_PLANS:
            return part
    return "unknown"


def parse_platform_time(value: str | None) -> datetime | None:
    """平台时间不带时区，按 UTC 存；真要展示北京时间时再做转换。"""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def card_is_ready(card: Card) -> bool:
    """有完整三要素、申请成功、次数没用完，才能拿去交易。"""
    return (
        card.apply_status == "SUCCESS"
        and card.used_count < MAX_USES
        and bool(card.card_no and card.expiry and card.cvv)
    )


# --------------------------------------------------------------------------- 开卡事件


def handle_card_issue(
    session: Session,
    event: CardIssueEvent,
    *,
    webhook_secret: str | None,
) -> None:
    if event.event_type == "CARD_ISSUE.FAILED":
        _handle_issue_failed(session, event)
        return
    if not event.data.cards:
        logger.error(
            "开卡成功事件没有卡片明细 event=%s requestNo=%s applyStatus=%s",
            event.event_id,
            event.data.request_no,
            event.data.apply_status,
        )
        return
    for issued in event.data.cards:
        _upsert_issued_card(session, event, issued, webhook_secret)


def _upsert_issued_card(
    session: Session,
    event: CardIssueEvent,
    issued: IssuedCard,
    webhook_secret: str | None,
) -> None:
    data = event.data
    card = crud.get_card_by_platform_id(session, issued.card_id)
    if card is None and data.request_no:
        # 占位行还没写 card_id，用请求号认出它
        card = crud.get_card_by_request_no(session, data.request_no)
    if card is None:
        logger.warning(
            "开卡事件找不到本地占位卡，按 webhook 补建 card=%s requestNo=%s",
            issued.card_id,
            data.request_no,
        )
        request_no = data.request_no or f"webhook_{event.event_id}"
        if crud.get_card_by_request_no(session, request_no) is not None:
            request_no = f"webhook_{event.event_id}"
        card = Card(
            plan=parse_plan_from_request_no(data.request_no),
            request_no=request_no[:64],
            apply_status="SUCCESS",
            enabled=False,
        )

    card.card_id = issued.card_id
    card.batch_no = data.batch_no or card.batch_no
    card.apply_status = "SUCCESS"
    if data.initial_funding_status:
        card.initial_funding_status = data.initial_funding_status
    card.applied_at = (
        parse_platform_time(data.complete_time)
        or parse_platform_time(issued.created_at)
        or utcnow()
    )
    if card.balance <= 0 and data.initial_funding_pending_amount is not None:
        card.balance = Decimal(data.initial_funding_pending_amount)

    _apply_card_secrets(card, issued, webhook_secret)

    if data.initial_funding_status == "FAILED":
        logger.error(
            "开卡首充失败 card=%s requestNo=%s，卡暂不可用",
            card.card_id,
            card.request_no,
        )
        card.enabled = False
    else:
        card.enabled = card_is_ready(card)

    crud.save_card(session, card)
    logger.info(
        "开卡事件已落库 card=%s plan=%s 启用=%s 次数=%d 余额=%s",
        card.card_id,
        card.plan,
        card.enabled,
        card.used_count,
        card.balance,
    )


def _apply_card_secrets(card: Card, issued: IssuedCard, webhook_secret: str | None) -> None:
    if not issued.card_number_ciphertext:
        logger.error("开卡事件缺少卡号密文，卡暂不可用 card=%s", issued.card_id)
        return
    if not webhook_secret:
        logger.error("未配置 AILINK_WEBHOOK_SECRET，无法解密卡三要素 card=%s", issued.card_id)
        return
    try:
        secrets = issued.decrypt(webhook_secret)
    except AiLinkDecryptError as exc:
        logger.error("开卡三要素解密失败 card=%s：%s", issued.card_id, exc)
        return
    card.card_no = secrets.card_number
    card.expiry = secrets.expiry_date
    card.cvv = secrets.cvv


def _handle_issue_failed(session: Session, event: CardIssueEvent) -> None:
    data = event.data
    card = crud.get_card_by_request_no(session, data.request_no) if data.request_no else None
    if card is None:
        logger.error(
            "开卡失败事件找不到本地占位卡 requestNo=%s event=%s",
            data.request_no,
            event.event_id,
        )
    else:
        crud.mark_card_failed(session, card)
    for item in data.fail_items:
        logger.error(
            "开卡失败 requestNo=%s itemNo=%s fundStatus=%s code=%s reason=%s",
            data.request_no,
            item.item_no,
            item.fund_status,
            item.fail_reason_code,
            item.fail_reason_text,
        )


# --------------------------------------------------------------------------- 交易事件


def handle_card_transaction(session: Session, event: CardTransactionEvent) -> None:
    data = event.data
    if not data.transaction_id:
        logger.error("交易事件缺少 transactionId event=%s", event.event_id)
        return
    status = (data.event_status or "").upper()

    transaction = crud.get_card_transaction(session, data.transaction_id)
    if transaction is None:
        transaction = CardTransaction(
            transaction_id=data.transaction_id,
            card_id=data.card_id or "",
            event_status=status or "UNKNOWN",
        )
    transaction.event_id = event.event_id
    if data.card_id:
        transaction.card_id = data.card_id
    if status:
        transaction.event_status = status
    if data.original_amount is not None:
        transaction.original_amount = data.original_amount
    if data.actual_amount is not None:
        transaction.actual_amount = data.actual_amount
    if data.merchant_name:
        transaction.merchant_name = data.merchant_name
    if data.related_transaction_id:
        transaction.related_transaction_id = data.related_transaction_id
    parsed_time = parse_platform_time(data.transaction_time)
    if parsed_time is not None:
        transaction.transaction_time = parsed_time

    card = (
        crud.get_card_by_platform_id(session, transaction.card_id) if transaction.card_id else None
    )
    if card is None:
        logger.error(
            "交易事件找不到本地卡 card=%s transaction=%s 商户=%s 金额=%s %s",
            transaction.card_id,
            transaction.transaction_id,
            transaction.merchant_name,
            transaction.actual_amount,
            data.actual_currency,
        )
        crud.save_card_transaction(session, transaction)
        return

    if status in COUNT_DELTA and not transaction.use_counted:
        delta = COUNT_DELTA[status]
        before = card.used_count
        card.used_count = max(0, before + delta)
        transaction.use_counted = True
        logger.info(
            "卡使用次数变更 card=%s transaction=%s %+d：%d -> %d",
            card.card_id,
            transaction.transaction_id,
            delta,
            before,
            card.used_count,
        )

    if status in BALANCE_DELTA and not transaction.balance_applied:
        amount = transaction.actual_amount or Decimal("0")
        after = card.balance + amount * BALANCE_DELTA[status]
        if after < 0:
            logger.error(
                "卡余额不足：card=%s balance=%s 本次扣减=%s transaction=%s",
                card.card_id,
                card.balance,
                amount,
                transaction.transaction_id,
            )
        card.balance = max(Decimal("0"), after)
        transaction.balance_applied = True

    if status == "AUTH_FAILED":
        logger.error(
            "交易授权失败 card=%s transaction=%s 商户=%s 金额=%s %s",
            card.card_id,
            transaction.transaction_id,
            transaction.merchant_name,
            transaction.actual_amount,
            data.actual_currency,
        )

    card.enabled = card_is_ready(card)
    crud.save_card_transaction(session, transaction, card)
