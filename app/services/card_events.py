"""Webhook 事件落库：开卡补三要素、交易计数与余额。

余额与首充口径：开卡时余额为 0；首充是独立异步流程，只由
CARD_OPERATION.RECHARGE_SUCCESS 入账，成功前卡不启用。

计数与余额口径（同一 transaction 多次推送，按状态目标值做差值，重试不重复）：
- PROCESSING：次数 +1，并按 actual_amount 扣卡余额（预先占用）；
- AUTH_SUCCESS / SETTLED：只推进状态，次数和余额都不再变；
- AUTH_FAILED：次数 -1，并退回 PROCESSING 已扣的金额；
- REFUND_SUCCESS / REVERSE_SUCCESS（独立交易）：次数 -1，并加回 actual_amount；
- CORRECTION：修正金额按负数继续扣，不影响使用次数；
- 次数到 3 自动停用，降回 3 以下会重新启用。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlmodel import Session

from app import crud
from app.integrations.ailink.errors import AiLinkDecryptError
from app.integrations.ailink.events import (
    CardIssueEvent,
    CardOperationEvent,
    CardTransactionEvent,
    IssuedCard,
)
from app.models import Card, CardOperation, CardPlan, CardTransaction, utcnow

logger = logging.getLogger(__name__)

MAX_USES = 3

# 状态变化时对使用次数的增量：PROCESSING 占一次，失败/退款回退一次。
# AUTH_SUCCESS / SETTLED / CORRECTION 不再额外加；同一状态重复推送不重复应用。
COUNT_DELTA: dict[str, int] = {
    "PROCESSING": 1,
    "AUTH_FAILED": -1,
    "REFUND_SUCCESS": -1,
    "REVERSE_SUCCESS": -1,
}

# 该交易对卡余额的目标累计金额符号：
# PROCESSING 预先扣；AUTH_SUCCESS/SETTLED 不再扣；失败退回；修正继续扣。
BALANCE_TARGET: dict[str, int] = {
    "PROCESSING": -1,
    "AUTH_FAILED": 0,
    "REFUND_SUCCESS": 1,
    "REVERSE_SUCCESS": 1,
    "CORRECTION": -1,
}

_KNOWN_PLANS = {plan.value for plan in CardPlan}


def parse_plan_from_request_no(request_no: str | None) -> str:
    """requestNo 形如 kaika_plus_1730000000000ab12；认不出套餐时默认 plus。"""
    for part in (request_no or "").split("_"):
        if part in _KNOWN_PLANS:
            return part
    return "plus"


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
    # 首充是独立异步流程：issue 事件不记余额，只记录状态；已经有 SUCCESS（充值先到）就不回退
    if data.initial_funding_status and card.initial_funding_status != "SUCCESS":
        card.initial_funding_status = data.initial_funding_status
    if card.initial_funding_status is None:
        card.initial_funding_status = "PENDING"
    card.applied_at = (
        parse_platform_time(data.complete_time)
        or parse_platform_time(issued.created_at)
        or utcnow()
    )

    _apply_card_secrets(card, issued, webhook_secret)

    if data.initial_funding_status == "FAILED":
        logger.error(
            "开卡首充失败 card=%s requestNo=%s，卡暂不可用",
            card.card_id,
            card.request_no,
        )
        card.enabled = False
    else:
        # 首充成功前不启用，余额只等 CARD_OPERATION.RECHARGE_SUCCESS 入账
        card.enabled = card_is_ready(card) and card.initial_funding_status == "SUCCESS"

    crud.save_card(session, card)
    logger.info(
        "开卡事件已落库 card=%s plan=%s 启用=%s 次数=%d 余额=%s 首充=%s",
        card.card_id,
        card.plan,
        card.enabled,
        card.used_count,
        card.balance,
        card.initial_funding_status,
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
    is_new = transaction is None
    if transaction is None:
        transaction = CardTransaction(
            transaction_id=data.transaction_id,
            card_id=data.card_id or "",
            event_status="UNKNOWN",
        )
    prev_status = None if is_new else transaction.event_status
    prev_actual = Decimal("0") if is_new else (transaction.actual_amount or Decimal("0"))
    transaction.event_id = event.event_id
    if data.card_id:
        transaction.card_id = data.card_id
    if status:
        transaction.event_status = status
    if data.original_amount is not None:
        transaction.original_amount = data.original_amount
    if data.actual_amount is not None:
        transaction.actual_amount = data.actual_amount
    if data.original_currency:
        transaction.original_currency = data.original_currency
    if data.actual_currency:
        transaction.actual_currency = data.actual_currency
    if data.merchant_name:
        transaction.merchant_name = data.merchant_name
    platform_remark = data.remark or data.fail_reason_text or data.fail_reason_code or data.reason
    if platform_remark:
        transaction.remark = platform_remark[:255]
    elif status == "AUTH_FAILED":
        transaction.remark = "授权失败"
    if data.related_transaction_id:
        transaction.related_transaction_id = data.related_transaction_id
    parsed_time = parse_platform_time(data.transaction_time)
    if parsed_time is not None:
        transaction.transaction_time = parsed_time

    card = (
        crud.get_card_by_platform_id(session, transaction.card_id, for_update=True)
        if transaction.card_id
        else None
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

    if status != prev_status and status in COUNT_DELTA:
        delta = COUNT_DELTA[status]
        before = card.used_count
        card.used_count = max(0, before + delta)
        transaction.use_counted = delta > 0
        logger.info(
            "卡使用次数变更 card=%s transaction=%s %s %+d：%d -> %d",
            card.card_id,
            transaction.transaction_id,
            status,
            delta,
            before,
            card.used_count,
        )

    if status in BALANCE_TARGET:
        previous = BALANCE_TARGET.get(prev_status, 0) * prev_actual
        target = BALANCE_TARGET[status] * (transaction.actual_amount or Decimal("0"))
        delta = target - previous
        if delta:
            after = card.balance + delta
            if after < 0:
                logger.error(
                    "卡余额不足：card=%s balance=%s 本次扣减=%s transaction=%s status=%s",
                    card.card_id,
                    card.balance,
                    -delta,
                    transaction.transaction_id,
                    status,
                )
            card.balance = max(Decimal("0"), after)
            logger.info(
                "卡余额变更 card=%s transaction=%s %s %+.2f：%s -> %s",
                card.card_id,
                transaction.transaction_id,
                status,
                delta,
                card.balance - delta,
                card.balance,
            )
        transaction.balance_applied = target != 0

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


# --------------------------------------------------------------------------- 卡操作事件

# 资金操作对余额的方向；其他操作只记流水，不动余额
OPERATION_BALANCE_EFFECT: dict[str, int] = {
    "CARD_OPERATION.RECHARGE_SUCCESS": 1,
    "CARD_OPERATION.WITHDRAW_SUCCESS": -1,
}


def handle_card_operation(session: Session, event: CardOperationEvent) -> None:
    """充值/转出在同一个事务里写流水并更新余额，eventId 唯一保证重投幂等。"""
    data = event.data
    status = (data.operation_status or "").upper()
    amount = data.amount or Decimal("0")
    card = (
        crud.get_card_by_platform_id(session, data.card_id, for_update=True)
        if data.card_id
        else None
    )
    if card is None and data.card_issue_request_no:
        # 首充可能比开卡 webhook 先到：占位行还没有 card_id，用开卡请求号认领
        card = crud.get_card_by_request_no(
            session,
            data.card_issue_request_no,
            for_update=True,
        )
        if card is not None and card.card_id is None and data.card_id:
            card.card_id = data.card_id

    if card is None:
        logger.error(
            "卡操作事件找不到本地卡 card=%s event=%s type=%s",
            data.card_id,
            event.event_id,
            event.event_type,
        )
    else:
        effect = OPERATION_BALANCE_EFFECT.get(event.event_type, 0)
        if effect:
            after = card.balance + amount * effect
            if after < 0:
                logger.error(
                    "卡余额不足：card=%s balance=%s 操作=%s 金额=%s %s",
                    card.card_id,
                    card.balance,
                    event.event_type,
                    amount,
                    data.currency,
                )
            card.balance = max(Decimal("0"), after)
            logger.info(
                "卡余额变更 card=%s event=%s %s %+.2f：%s -> %s",
                card.card_id,
                event.event_id,
                event.event_type,
                amount * effect,
                card.balance - amount * effect,
                card.balance,
            )

        if data.funding_purpose == "INITIAL_FUNDING":
            if event.event_type == "CARD_OPERATION.RECHARGE_SUCCESS":
                card.initial_funding_status = data.initial_funding_status or "SUCCESS"
                # 首充到账才启用；三要素还没补齐的话 card_is_ready 仍是 False
                card.enabled = card_is_ready(card)
            elif event.event_type == "CARD_OPERATION.RECHARGE_FAILED":
                card.initial_funding_status = "FAILED"
                card.enabled = False
                logger.error(
                    "开卡首充失败 card=%s requestNo=%s",
                    card.card_id,
                    data.card_issue_request_no,
                )

    operation = CardOperation(
        event_id=event.event_id,
        card_id=data.card_id or "",
        operation_type=data.operation_type or event.event_type.split(".", 1)[-1],
        operation_status=status or "UNKNOWN",
        amount=amount,
        currency=data.currency,
        request_no=data.request_no,
        funding_purpose=data.funding_purpose,
        card_issue_request_no=data.card_issue_request_no,
        operation_time=parse_platform_time(data.operation_time),
    )
    if not crud.apply_card_operation(session, operation, card):
        logger.info("卡操作事件重复投递，跳过：event=%s", event.event_id)
