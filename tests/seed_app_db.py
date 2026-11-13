"""往 app.db 写一份演示数据，方便直接打开表看效果。

- 可重复执行：按 request_no / transaction_id 去重，不删任何已有数据。
- 走真实的 services 逻辑（开卡 webhook、交易 webhook），计数和余额跟线上口径一致。
- 用法：uv run python -m tests.seed_app_db
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlmodel import select

from app import crud
from app.config import get_settings
from app.db import session_scope
from app.integrations.ailink.crypto import derive_sensitive_key
from app.integrations.ailink.webhooks import parse_event
from app.models import Card, CardTransaction
from app.services.card_events import (
    handle_card_issue,
    handle_card_operation,
    handle_card_transaction,
)
from app.services.card_stock import PLAN_SPECS

DEMO_SECRET = "demo_webhook_secret"
DEMO_CARD_NUMBER = "4865321234561298"

# 套餐、请求号、平台卡 ID、开卡时间；首充金额统一取 PLAN_SPECS 的 单价 × 3，避免两处配置漂移
DEMO_CARDS = (
    ("plus", "kaika_plus_demo0001", "VC_DEMO_PLUS", "2026-11-01 10:00:00"),
    ("pro100", "kaika_pro100_demo0001", "VC_DEMO_PRO100", "2026-11-01 10:05:00"),
    ("pro200", "kaika_pro200_demo0001", "VC_DEMO_PRO200", "2026-11-01 10:10:00"),
    ("pro500", "kaika_pro500_demo0001", "VC_DEMO_PRO500", "2026-11-01 10:15:00"),
)

SPEC_BY_PLAN = {spec.name: spec for spec in PLAN_SPECS}

# (transaction_id, card_id, status, original, actual, merchant, 时间, related)
DEMO_TRANSACTIONS = (
    (
        "txn_demo_plus_1",
        "VC_DEMO_PLUS",
        "AUTH_SUCCESS",
        "35.60",
        "36.13",
        "Google Ads",
        "2026-11-01 11:00:00",
        "",
    ),
    (
        "txn_demo_plus_1",
        "VC_DEMO_PLUS",
        "SETTLED",
        "35.60",
        "36.13",
        "Google Ads",
        "2026-11-01 11:00:00",
        "",
    ),
    (
        "txn_demo_pro100_1",
        "VC_DEMO_PRO100",
        "AUTH_SUCCESS",
        "10.00",
        "10.06",
        "OpenAI",
        "2026-11-02 09:30:00",
        "",
    ),
    (
        "txn_demo_pro100_2",
        "VC_DEMO_PRO100",
        "AUTH_FAILED",
        "12.00",
        "0.00",
        "Example Store",
        "2026-11-02 10:10:00",
        "",
    ),
    (
        "txn_demo_pro200_1",
        "VC_DEMO_PRO200",
        "AUTH_SUCCESS",
        "60.00",
        "60.20",
        "Meta Ads",
        "2026-11-03 14:00:00",
        "",
    ),
    (
        "txn_demo_pro200_1",
        "VC_DEMO_PRO200",
        "SETTLED",
        "60.00",
        "60.20",
        "Meta Ads",
        "2026-11-03 14:00:00",
        "",
    ),
    (
        "txn_demo_pro200_2",
        "VC_DEMO_PRO200",
        "AUTH_SUCCESS",
        "40.00",
        "40.15",
        "Google Ads",
        "2026-11-03 15:20:00",
        "",
    ),
    (
        "txn_demo_pro200_2",
        "VC_DEMO_PRO200",
        "SETTLED",
        "40.00",
        "40.15",
        "Google Ads",
        "2026-11-03 15:20:00",
        "",
    ),
    (
        "txn_demo_pro200_refund",
        "VC_DEMO_PRO200",
        "REFUND_SUCCESS",
        "20.00",
        "20.00",
        "Google Ads",
        "2026-11-03 16:00:00",
        "txn_demo_pro200_2",
    ),
    (
        "txn_demo_pro500_1",
        "VC_DEMO_PRO500",
        "PROCESSING",
        "99.00",
        "0.00",
        "Google Ads",
        "2026-11-04 09:00:00",
        "",
    ),
)


def _encrypt(value: str, secret: str) -> str:
    key = derive_sensitive_key(secret)
    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, value.encode(), None)
    return base64.urlsafe_b64encode(iv + sealed).decode().rstrip("=")


def _event_body(event_id: str, event_type: str, category: str, data: dict) -> bytes:
    payload = {
        "eventId": event_id,
        "eventType": event_type,
        "eventCategory": category,
        "eventTime": "2026-11-01 10:20:00",
        "apiVersion": "2026-06-01",
        "merchantId": 10001,
        "data": data,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _issue_event(plan: str, request_no: str, card_id: str, amount: str, applied_at: str):
    card = {
        "cardId": card_id,
        "cardNoMask": "486532******1298",
        "cardType": "PREPAID",
        "cardStatus": 1,
        "remark": f"stock-{plan}",
        "effectiveLimitConfig": {
            "perTransLimit": "0.00",
            "dayLimit": "0.00",
            "monthLimit": "0.00",
            "cardLifeCycleLimit": amount,
            "currency": "USD",
        },
        "cardNumberCiphertext": _encrypt(DEMO_CARD_NUMBER, DEMO_SECRET),
        "expiryDateCiphertext": _encrypt("31/10", DEMO_SECRET),
        "cvvCiphertext": _encrypt("123", DEMO_SECRET),
        "sensitiveEncryptAlg": "AES-256-GCM",
        "sensitiveEncryptVersion": "v1",
        "issueBatchNo": f"BATCH_{plan.upper()}",
        "createdAt": applied_at,
    }
    data = {
        "taskId": 9001,
        "batchNo": f"BATCH_{plan.upper()}",
        "requestNo": request_no,
        "cardType": "PREPAID",
        "applyStatus": "SUCCESS",
        "fundStatus": "CONFIRMED",
        "deliveryMode": "POOL_IMMEDIATE",
        "initialFundingStatus": "PENDING",
        "cardIds": [card_id],
        "totalCount": 1,
        "successCount": 1,
        "failCount": 0,
        "cards": [card],
        "failItems": [],
        "completeTime": applied_at,
        "updateTime": applied_at,
    }
    return parse_event(
        _event_body(f"evt_demo_issue_{plan}", "CARD_ISSUE.SUCCESS", "CARD_ISSUE", data)
    )


def _funding_event(plan: str, request_no: str, card_id: str, amount: str):
    data = {
        "requestNo": f"CR_{plan.upper()}_DEMO",
        "cardIssueRequestNo": request_no,
        "fundingPurpose": "INITIAL_FUNDING",
        "initialFundingStatus": "SUCCESS",
        "operationType": "RECHARGE",
        "operationStatus": "SUCCESS",
        "cardId": card_id,
        "cardNoMask": "486532******1298",
        "cardType": "PREPAID",
        "amount": amount,
        "currency": "USD",
        "operationTime": "2026-11-01 10:02:00",
    }
    return parse_event(
        _event_body(
            f"evt_demo_funding_{plan}",
            "CARD_OPERATION.RECHARGE_SUCCESS",
            "CARD_OPERATION",
            data,
        )
    )


def _transaction_event(
    transaction_id: str,
    card_id: str,
    status: str,
    original: str,
    actual: str,
    merchant: str,
    occurred_at: str,
    related: str,
):
    data = {
        "transactionId": transaction_id,
        "relatedTransactionId": related,
        "eventStatus": status,
        "cardId": card_id,
        "cardNoMask": "486532******1298",
        "cardType": "PREPAID",
        "transactionType": (
            "REFUND" if "REFUND" in status else "REVERSE" if "REVERSE" in status else "PURCHASE"
        ),
        "originalAmount": original,
        "originalCurrency": "USD",
        "actualAmount": actual,
        "actualCurrency": "USD",
        "feeAmount": "0.53" if status not in {"AUTH_FAILED", "PROCESSING"} else "0.00",
        "merchantName": merchant,
        "merchantCountry": "US",
        "merchantMcc": "7311",
        "transactionTime": occurred_at,
    }
    event_type = f"CARD_TRANSACTION.{status}"
    return parse_event(
        _event_body(
            f"evt_demo_{transaction_id}_{status}",
            event_type,
            "CARD_TRANSACTION",
            data,
        )
    )


def seed() -> None:
    settings = get_settings()
    print(f"写入演示数据到 {settings.database_url}")

    with session_scope() as session:
        for plan, request_no, card_id, applied_at in DEMO_CARDS:
            amount = f"{SPEC_BY_PLAN[plan].initial_amount:.2f}"
            if crud.get_card_by_request_no(session, request_no) is not None:
                print(f"跳过已存在的卡 {plan} {request_no}")
                continue
            crud.create_card_intent(
                session,
                plan=plan,
                request_no=request_no,
            )
            handle_card_issue(
                session,
                _issue_event(plan, request_no, card_id, amount, applied_at),
                webhook_secret=DEMO_SECRET,
            )
            handle_card_operation(
                session,
                _funding_event(plan, request_no, card_id, amount),
            )
            print(f"已开卡 {plan} card_id={card_id} 首充={amount}")

        for row in DEMO_TRANSACTIONS:
            transaction_id, card_id, status, original, actual, merchant, occurred_at, related = row
            handle_card_transaction(
                session,
                _transaction_event(
                    transaction_id,
                    card_id,
                    status,
                    original,
                    actual,
                    merchant,
                    occurred_at,
                    related,
                ),
            )
            print(f"已写入交易 {transaction_id} {status} 金额={actual} 商户={merchant}")

        cards = session.exec(select(Card).where(Card.plan.in_([c[0] for c in DEMO_CARDS]))).all()
        print("\n当前演示卡：")
        print(f"{'plan':<8}{'card_id':<16}{'余额':>10}{'次数':>6}{'启用':>6}  卡号")
        for card in cards:
            print(
                f"{card.plan:<8}{card.card_id or '-':<16}"
                f"{card.balance:>10}{card.used_count:>6}{str(card.enabled):>6}  {card.card_no}"
            )

        transactions = session.exec(select(CardTransaction).order_by(CardTransaction.id)).all()
        print(f"\n交易流水 {len(transactions)} 条：")
        print(f"{'transaction_id':<26}{'状态':<16}{'实际金额':>10}  商户")
        for item in transactions:
            print(
                f"{item.transaction_id:<26}{item.event_status:<16}"
                f"{item.actual_amount:>10}  {item.merchant_name}"
            )


if __name__ == "__main__":
    seed()
