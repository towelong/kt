"""AiLink VCC 请求 / 响应模型。

- 金额一律 Decimal：接口收发的都是字符串，用 float 会出现 0.1 + 0.2 之类的分账误差。
- 出参序列化成两位小数字符串（`"100.00"`），和文档示例一致。
- 请求模型 `extra="forbid"`，字段名写错当场报错，而不是被平台静默忽略。
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from app.integrations.ailink.errors import AiLinkProtocolError

logger = logging.getLogger(__name__)

_REQUEST_NO_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{7,63}")


def _money_to_str(value: Decimal) -> str:
    return f"{value:.2f}"


Money = Annotated[
    Decimal,
    Field(max_digits=18, decimal_places=2),
    PlainSerializer(_money_to_str, return_type=str, when_used="json"),
]
PositiveMoney = Annotated[
    Decimal,
    Field(gt=0, max_digits=18, decimal_places=2),
    PlainSerializer(_money_to_str, return_type=str, when_used="json"),
]


class AiLinkModel(BaseModel):
    """接口字段统一 camelCase；容忍平台新增字段，避免多一个字段就整单失败。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class AiLinkRequest(AiLinkModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


ModelT = TypeVar("ModelT", bound=BaseModel)


def validate_payload(model: type[ModelT], data: Any, what: str) -> ModelT:
    """把响应/事件数据塞进模型；失败只报字段位置，不回显字段值（可能含敏感数据）。"""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        details = ", ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}:{err['type']}"
            for err in exc.errors()
        )
        raise AiLinkProtocolError(f"AiLink VCC {what} 响应结构不符合预期（{details}）") from exc


class ApiEnvelope(AiLinkModel):
    """统一响应外层：code=200 才算受理，data 里再判断业务状态。"""

    code: int
    msg: str = ""
    request_id: str | None = None
    data: dict[str, Any] | None = None


# --------------------------------------------------------------------------- 获取卡 BIN


class CardBinQuota(AiLinkModel):
    max_card_count: int
    issued_card_count: int
    remaining_card_count: int


class CardBin(AiLinkModel):
    bin_id: int
    bin: str
    card_org: str
    card_type: str
    currency: str
    issuing_country: str | None = None
    supported_countries: list[str] = Field(default_factory=list)
    max_open_card_count: int | None = None
    min_open_card_amount: Decimal | None = None
    min_recharge_amount: Decimal | None = None
    max_card_count: int | None = None
    issued_card_count: int | None = None

    @field_validator("supported_countries", mode="before")
    @classmethod
    def _parse_supported_countries(cls, value: Any) -> Any:
        """文档里是 JSON 字符串；解析失败就退化为空列表，不让整批 BIN 作废。"""
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("VCC 卡 BIN supportedCountries 不是合法 JSON，按空列表处理")
            return []
        if not isinstance(parsed, list):
            logger.warning("VCC 卡 BIN supportedCountries 不是数组，按空列表处理")
            return []
        return [str(item) for item in parsed]


class CardBinResult(AiLinkModel):
    quota: CardBinQuota
    bins: list[CardBin] = Field(default_factory=list)


# --------------------------------------------------------------------------- 账户余额


class CurrencyBalance(AiLinkModel):
    currency: str
    balance: Decimal
    frozen_amount: Decimal
    available_balance: Decimal


class AccountBalanceResult(AiLinkModel):
    balances: list[CurrencyBalance] = Field(default_factory=list)

    def get(self, currency: str) -> CurrencyBalance | None:
        target = currency.upper()
        return next((item for item in self.balances if item.currency.upper() == target), None)


class ConvertRequest(AiLinkRequest):
    from_currency: Literal["USD", "USDT"]
    to_currency: Literal["USD", "USDT"]
    amount: PositiveMoney

    @model_validator(mode="after")
    def _check_currencies(self) -> ConvertRequest:
        if self.from_currency == self.to_currency:
            raise ValueError("fromCurrency 和 toCurrency 不能相同")
        return self


class BalanceConvertResult(AiLinkModel):
    order_no: str
    from_currency: str
    to_currency: str
    amount: Decimal
    fee_rate: Decimal
    fee_amount: Decimal
    received_amount: Decimal


# --------------------------------------------------------------------------- 申请卡


class LimitConfig(AiLinkRequest):
    """预算卡限额。单笔独立，日/月/生命周期同次只允许一种为正数。"""

    per_trans_limit: Money = Decimal("0")
    day_limit: Money = Decimal("0")
    month_limit: Money = Decimal("0")
    card_life_cycle_limit: Money = Decimal("0")
    currency: str = "USD"

    @model_validator(mode="after")
    def _check_period_limits(self) -> LimitConfig:
        for name in ("per_trans_limit", "day_limit", "month_limit", "card_life_cycle_limit"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不能为负数")
        periods = [
            name
            for name in ("day_limit", "month_limit", "card_life_cycle_limit")
            if getattr(self, name) > 0
        ]
        if len(periods) > 1:
            raise ValueError("dayLimit / monthLimit / cardLifeCycleLimit 同次只能有一个为正数")
        return self


class CardApplyRequest(AiLinkRequest):
    request_no: str
    card_type: Literal["PREPAID", "BUDGET"]
    card_bin_id: int = Field(gt=0)
    holder_id: int | None = Field(default=None, gt=0)
    card_count: int | None = Field(default=1, ge=1)
    recharge_amount: PositiveMoney | None = None
    card_group_id: int | None = Field(default=None, gt=0)
    budget_id: int | None = Field(default=None, gt=0)
    remark: str | None = Field(default=None, max_length=40)
    limit_config: LimitConfig | None = None

    @field_validator("request_no")
    @classmethod
    def _check_request_no(cls, value: str) -> str:
        if not _REQUEST_NO_RE.fullmatch(value):
            raise ValueError("requestNo 必须 8-64 位、字母开头、只含字母数字下划线和中横线")
        if not any(char.isdigit() for char in value):
            raise ValueError("requestNo 必须同时包含字母和数字")
        return value

    @model_validator(mode="after")
    def _check_card_type_fields(self) -> CardApplyRequest:
        if self.card_type == "PREPAID":
            if self.recharge_amount is None:
                raise ValueError("储值卡必须传 rechargeAmount（单卡首充金额）")
            if self.card_group_id is not None or self.budget_id is not None:
                raise ValueError("储值卡不能传 cardGroupId / budgetId")
            if self.limit_config is not None:
                raise ValueError("储值卡不能传 limitConfig")
        else:
            if self.card_group_id is None or self.budget_id is None:
                raise ValueError("预算卡必须传 cardGroupId 和 budgetId")
            if self.recharge_amount is not None:
                raise ValueError("预算卡不能传 rechargeAmount")
            if self.card_count not in (None, 1):
                raise ValueError("预算卡建议单张申请")
        return self


class CardApplyResult(AiLinkModel):
    task_id: int
    batch_no: str
    request_no: str
    apply_status: str
    apply_status_text: str | None = None
    task_status: str | None = None
    task_status_text: str | None = None
    delivery_mode: str | None = None
    initial_funding_status: str | None = None
    initial_funding_pending_count: int | None = None
    initial_funding_failed_count: int | None = None
    initial_funding_pending_amount: Decimal | None = None
    card_ids: list[str] = Field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.apply_status == "SUCCESS"

    @property
    def initial_funding_pending(self) -> bool:
        """首充还在 PENDING：开卡成功不等于钱到账，等 CARD_OPERATION.RECHARGE_SUCCESS。"""
        return self.initial_funding_status == "PENDING"
