"""可复用的测试数据和假客户端，后续业务测试直接从这里取。

约定：不清理数据、不写项目库；所有用例都用临时 SQLite。
"""

from decimal import Decimal

from app import crud
from app.integrations.ailink.models import (
    AccountBalanceResult,
    BalanceConvertResult,
    CardApplyResult,
    CardBin,
    CardBinQuota,
    CardBinResult,
    CurrencyBalance,
)
from app.models import Card
from app.services.card_stock import PLAN_SPECS

WEBHOOK_SECRET = "whsec_test_secret"
REQUEST_NO = "kaika_plus_1730000000000ab12"
CARD_NUMBER = "4865321234561298"


# --------------------------------------------------------------------------- 卡与事件


def make_card(
    session,
    *,
    plan="plus",
    card_id="VC1",
    request_no=REQUEST_NO,
    card_no=CARD_NUMBER,
    used_count=0,
    enabled=True,
    balance=Decimal("48.00"),
) -> Card:
    card = crud.create_card_intent(
        session,
        plan=plan,
        request_no=request_no,
        recharge_amount=balance,
    )
    card.card_id = card_id
    card.card_no = card_no
    card.expiry = "12/29"
    card.cvv = "123"
    card.used_count = used_count
    card.enabled = enabled
    card.apply_status = "SUCCESS"
    return crud.save_card(session, card)


def add_usable_card(session, plan: str) -> Card:
    return make_card(
        session,
        plan=plan,
        card_id=f"VC_{plan}",
        request_no=f"kaika_{plan}_seed0001",
        balance=Decimal("1.00"),
    )


def cover_all_plans(session) -> None:
    for spec in PLAN_SPECS:
        add_usable_card(session, spec.name)


def parse(make_body, event_id, event_type, category, data):
    from app.integrations.ailink.webhooks import parse_event

    return parse_event(
        make_body(
            {
                "eventId": event_id,
                "eventType": event_type,
                "eventCategory": category,
                "eventTime": "2026-07-02 12:00:00",
                "apiVersion": "2026-06-01",
                "merchantId": 10001,
                "data": data,
            }
        )
    )


def issue_event(
    make_body,
    encrypt_field,
    *,
    event_type="CARD_ISSUE.SUCCESS",
    request_no=REQUEST_NO,
    card_id="VC1",
    initial_funding="PENDING",
    with_secrets=True,
    event_id="evt_issue_1",
    secret=WEBHOOK_SECRET,
):
    card = {
        "cardId": card_id,
        "cardNoMask": "486532******1298",
        "cardType": "PREPAID",
        "cardStatus": 1,
        "remark": "stock-plus",
        "effectiveLimitConfig": {"cardLifeCycleLimit": "3000.00", "currency": "USD"},
        "cardNumberCiphertext": "",
        "expiryDateCiphertext": "",
        "cvvCiphertext": "",
        "sensitiveEncryptAlg": "AES-256-GCM",
        "sensitiveEncryptVersion": "v1",
        "createdAt": "2026-07-02 11:29:58",
    }
    if with_secrets:
        card["cardNumberCiphertext"] = encrypt_field(CARD_NUMBER, secret)
        card["expiryDateCiphertext"] = encrypt_field("12/29", secret)
        card["cvvCiphertext"] = encrypt_field("123", secret)

    success = event_type.endswith("SUCCESS")
    data = {
        "taskId": 901,
        "batchNo": "BATCH1",
        "requestNo": request_no,
        "cardType": "PREPAID",
        "applyStatus": "SUCCESS" if success else "FAILED",
        "fundStatus": "CONFIRMED" if success else "REFUNDED",
        "deliveryMode": "POOL_IMMEDIATE",
        "initialFundingStatus": initial_funding,
        "initialFundingPendingAmount": 48.00,
        "cardIds": [card_id] if success else [],
        "totalCount": 1,
        "successCount": 1 if success else 0,
        "failCount": 0 if success else 1,
        "cards": [card] if success else [],
        "failItems": (
            []
            if success
            else [
                {
                    "itemId": 8891,
                    "itemNo": "BATCH1-0001",
                    "status": "FAILED",
                    "fundStatus": "REFUNDED",
                    "failReasonCode": "CARD_ISSUE_FAILED",
                    "failReasonText": "开卡失败，已退款",
                }
            ]
        ),
        "completeTime": "2026-07-02 11:30:00",
        "updateTime": "2026-07-02 11:30:00",
    }
    return parse(make_body, event_id, event_type, "CARD_ISSUE", data)


def transaction_event(
    make_body,
    *,
    transaction_id="txn_1",
    status="AUTH_SUCCESS",
    card_id="VC1",
    actual="36.13",
    original="35.60",
    event_id="evt_tx_1",
    related="",
    original_currency="USD",
    actual_currency="USD",
    remark=None,
):
    data = {
        "transactionId": transaction_id,
        "relatedTransactionId": related,
        "eventStatus": status,
        "cardId": card_id,
        "cardNoMask": "486532******1298",
        "cardType": "PREPAID",
        "transactionType": "REFUND" if "REFUND" in status else "PURCHASE",
        "originalAmount": original,
        "originalCurrency": original_currency,
        "actualAmount": actual,
        "actualCurrency": actual_currency,
        "feeAmount": "0.53",
        "merchantName": "Google Ads",
        "merchantCountry": "US",
        "merchantMcc": "7311",
        "transactionTime": "2026-07-02 12:00:02",
    }
    if remark is not None:
        data["remark"] = remark
    return parse(make_body, event_id, f"CARD_TRANSACTION.{status}", "CARD_TRANSACTION", data)


# --------------------------------------------------------------------------- 假的 VCC 客户端


def us_bin(bin_id=1001, *, country="US", card_type="PREPAID", currency="USD", min_open="10.00"):
    return CardBin(
        bin_id=bin_id,
        bin="486880",
        card_org="VISA",
        card_type=card_type,
        currency=currency,
        issuing_country=country,
        min_open_card_amount=Decimal(min_open),
        min_recharge_amount=Decimal(min_open),
    )


class FakeClient:
    """只实现调度用到的四个接口，够单测跑完整个决策链路。"""

    def __init__(
        self,
        *,
        usd="0.00",
        usdt="0.00",
        bins=None,
        quota_remaining=100,
        apply_status="SUCCESS",
    ):
        self.usd = Decimal(usd)
        self.usdt = Decimal(usdt)
        self.bins = [us_bin()] if bins is None else bins
        self.quota_remaining = quota_remaining
        self.apply_status = apply_status
        self.calls: list[str] = []
        self.conversions: list[Decimal] = []
        self.applies: list = []

    async def get_account_balance(self):
        self.calls.append("balance")
        return AccountBalanceResult(
            balances=[
                CurrencyBalance(
                    currency="USD",
                    balance=self.usd,
                    frozen_amount=Decimal("0.00"),
                    available_balance=self.usd,
                ),
                CurrencyBalance(
                    currency="USDT",
                    balance=self.usdt,
                    frozen_amount=Decimal("0.00"),
                    available_balance=self.usdt,
                ),
            ]
        )

    async def convert_balance(self, from_currency, to_currency, amount, *, request_id=None):
        self.calls.append("convert")
        amount = Decimal(amount)
        self.conversions.append(amount)
        received = (amount * Decimal("0.997")).quantize(Decimal("0.01"))
        self.usdt -= amount
        self.usd += received
        return BalanceConvertResult(
            order_no="AC1",
            from_currency=from_currency,
            to_currency=to_currency,
            amount=amount,
            fee_rate=Decimal("0.003000"),
            fee_amount=amount - received,
            received_amount=received,
        )

    async def list_card_bins(self):
        self.calls.append("bins")
        return CardBinResult(
            quota=CardBinQuota(
                max_card_count=100,
                issued_card_count=100 - self.quota_remaining,
                remaining_card_count=self.quota_remaining,
            ),
            bins=self.bins,
        )

    async def apply_cards(self, request, *, request_id=None):
        self.calls.append("apply")
        self.applies.append(request)
        index = len(self.applies)
        return CardApplyResult(
            task_id=index,
            batch_no=f"BATCH{index}",
            request_no=request.request_no,
            apply_status=self.apply_status,
            initial_funding_status="PENDING",
            card_ids=[f"VC{index}"],
        )
