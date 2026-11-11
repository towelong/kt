"""AiLink VCC Webhook 事件模型。

- 外层字段固定，data 按事件家族解析成具体模型，处理器拿到的是类型化对象。
- 未知 eventType 落到 UnknownEvent：平台随时可能加事件，接收端不能因此 4xx/5xx。
- 卡号/有效期/CVV 只以密文出现，必须显式调用 decrypt() 才解，避免随手 repr 进日志。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import Field, SecretStr, field_validator

from app.integrations.ailink.crypto import (
    SUPPORTED_ALG,
    SUPPORTED_VERSION,
    decrypt_sensitive,
    derive_sensitive_key,
)
from app.integrations.ailink.errors import AiLinkDecryptError
from app.integrations.ailink.models import AiLinkModel

logger = logging.getLogger(__name__)


class WebhookEvent(AiLinkModel):
    event_id: str
    event_type: str
    event_category: str
    event_time: str
    api_version: str
    merchant_id: int
    data: Any = None


# --------------------------------------------------------------------------- 开卡事件


class EffectiveLimitConfig(AiLinkModel):
    per_trans_limit: Decimal | None = None
    day_limit: Decimal | None = None
    month_limit: Decimal | None = None
    card_life_cycle_limit: Decimal | None = None
    currency: str | None = None


class CardIssueFailItem(AiLinkModel):
    item_id: int | None = None
    item_no: str | None = None
    seq_no: int | None = None
    status: str | None = None
    task_status: str | None = None
    fund_status: str | None = None
    fail_reason_code: str | None = None
    fail_reason_text: str | None = None


@dataclass(frozen=True, repr=False)
class SensitiveCard:
    """解密后的卡三要素。repr 不输出明文，避免被日志/异常拼进去。"""

    card_id: str
    card_number: str = field(repr=False)
    expiry_date: str = field(repr=False)
    cvv: str = field(repr=False)

    def __repr__(self) -> str:
        return f"SensitiveCard(card_id={self.card_id!r}, 卡号/有效期/CVV 已隐藏)"


class IssuedCard(AiLinkModel):
    card_id: str
    card_no_mask: str
    card_type: str | None = None
    card_status: int | None = None
    remark: str | None = None
    effective_limit_config: EffectiveLimitConfig | None = None
    card_number_ciphertext: str | None = None
    expiry_date_ciphertext: str | None = None
    cvv_ciphertext: str | None = None
    sensitive_encrypt_alg: str = SUPPORTED_ALG
    sensitive_encrypt_version: str = SUPPORTED_VERSION
    issue_batch_no: str | None = None
    created_at: str | None = None

    def decrypt(self, webhook_secret: str) -> SensitiveCard:
        """显式解密三要素；Secret 不匹配会抛 AiLinkDecryptError。"""
        if not self.card_number_ciphertext:
            raise AiLinkDecryptError(f"卡 {self.card_id} 没有卡号密文，无法解密")
        key = derive_sensitive_key(webhook_secret)
        params = {
            "alg": self.sensitive_encrypt_alg,
            "version": self.sensitive_encrypt_version,
        }
        return SensitiveCard(
            card_id=self.card_id,
            card_number=decrypt_sensitive(self.card_number_ciphertext, key, **params),
            expiry_date=(
                decrypt_sensitive(self.expiry_date_ciphertext, key, **params)
                if self.expiry_date_ciphertext
                else ""
            ),
            cvv=(
                decrypt_sensitive(self.cvv_ciphertext, key, **params) if self.cvv_ciphertext else ""
            ),
        )


class CardIssueData(AiLinkModel):
    task_id: int | None = None
    batch_no: str | None = None
    request_no: str | None = None
    card_type: str | None = None
    budget_id: int | None = None
    card_group_id: int | None = None
    apply_status: str | None = None
    fund_status: str | None = None
    delivery_mode: str | None = None
    initial_funding_status: str | None = None
    initial_funding_pending_count: int | None = None
    initial_funding_failed_count: int | None = None
    initial_funding_pending_amount: Decimal | None = None
    card_ids: list[str] = Field(default_factory=list)
    total_count: int | None = None
    success_count: int | None = None
    fail_count: int | None = None
    cards: list[IssuedCard] = Field(default_factory=list)
    fail_items: list[CardIssueFailItem] = Field(default_factory=list)
    complete_time: str | None = None
    update_time: str | None = None

    def decrypt_cards(self, webhook_secret: str) -> list[SensitiveCard]:
        """解密本批次所有带密文的卡；单卡缺密文就跳过，不影响其它卡。"""
        decrypted: list[SensitiveCard] = []
        for card in self.cards:
            if not card.card_number_ciphertext:
                continue
            decrypted.append(card.decrypt(webhook_secret))
        return decrypted


class CardIssueEvent(WebhookEvent):
    data: CardIssueData


# --------------------------------------------------------------------------- 卡操作事件


class CardOperationData(AiLinkModel):
    request_no: str | None = None
    card_issue_request_no: str | None = None
    funding_purpose: str | None = None
    initial_funding_status: str | None = None
    operation_type: str | None = None
    operation_status: str | None = None
    card_id: str | None = None
    card_no_mask: str | None = None
    card_type: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    refund_amount: Decimal | None = None
    refund_currency: str | None = None
    cancel_type: str | None = None
    reason: str | None = None
    source_transaction_id: str | None = None
    before_limit_config: EffectiveLimitConfig | None = None
    after_limit_config: EffectiveLimitConfig | None = None
    before_remark: str | None = None
    after_remark: str | None = None
    remark: str | None = None
    operation_time: str | None = None


class CardOperationEvent(WebhookEvent):
    data: CardOperationData


# --------------------------------------------------------------------------- 卡交易事件


class CardTransactionData(AiLinkModel):
    transaction_id: str | None = None
    related_transaction_id: str | None = None
    event_status: str | None = None
    card_id: str | None = None
    card_no_mask: str | None = None
    card_type: str | None = None
    transaction_type: str | None = None
    original_amount: Decimal | None = None
    original_currency: str | None = None
    actual_amount: Decimal | None = None
    actual_currency: str | None = None
    fee_amount: Decimal | None = None
    merchant_name: str | None = None
    merchant_country: str | None = None
    merchant_mcc: str | None = None
    transaction_time: str | None = None


class CardTransactionEvent(WebhookEvent):
    data: CardTransactionData


# --------------------------------------------------------------------------- 3DS


class Card3dsData(AiLinkModel):
    otp_id: int | None = None
    card_id: str | None = None
    card_no_mask: str | None = None
    otp_code: SecretStr = SecretStr("")
    merchant_name: str | None = None
    original_amount: Decimal | None = None
    original_currency: str | None = None
    transaction_time: str | None = None


class Card3dsEvent(WebhookEvent):
    data: Card3dsData


# --------------------------------------------------------------------------- 预算事件


class BudgetOperationData(AiLinkModel):
    budget_id: int | None = None
    budget_no: str | None = None
    budget_name: str | None = None
    card_group_id: int | None = None
    card_group_name: str | None = None
    operation_type: str | None = None
    operation_status: str | None = None
    request_no: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    status: str | None = None
    operation_time: str | None = None


class BudgetOperationEvent(WebhookEvent):
    data: BudgetOperationData


# --------------------------------------------------------------------------- 测试事件 / 未知事件


class WebhookTestData(AiLinkModel):
    message: str | None = None


class WebhookTestEvent(WebhookEvent):
    data: WebhookTestData


class UnknownEvent(WebhookEvent):
    """未登记的事件类型：保留原始 data，仅日志告警，照样 ack ok。"""

    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("data", mode="before")
    @classmethod
    def _coerce_null_data(cls, value: Any) -> Any:
        return value if value is not None else {}


# 事件家族 -> 模型。按 eventType 第一个点前的部分归类，平台新增同族事件类型也能接住。
_EVENT_MODEL_BY_FAMILY: dict[str, type[WebhookEvent]] = {
    "CARD_ISSUE": CardIssueEvent,
    "CARD_OPERATION": CardOperationEvent,
    "CARD_TRANSACTION": CardTransactionEvent,
    "CARD_3DS": Card3dsEvent,
    "BUDGET_OPERATION": BudgetOperationEvent,
    "WEBHOOK_TEST": WebhookTestEvent,
    # 风控销卡 CARD.RISK_CANCELLED 的 eventCategory 是 CARD，但业务上属于卡操作
    "CARD": CardOperationEvent,
}


def event_model_for(event_type: str) -> type[WebhookEvent]:
    family = event_type.split(".", 1)[0]
    return _EVENT_MODEL_BY_FAMILY.get(family, UnknownEvent)
