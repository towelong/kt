"""模型层校验：卡型必填、requestNo 规则、金额精度、限额自相矛盾。"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.integrations.ailink.models import (
    CardApplyRequest,
    CardApplyResult,
    CardBinResult,
    ConvertRequest,
    LimitConfig,
)


def prepaid(**overrides):
    payload = {
        "request_no": "kaika_202606250001ab",
        "card_type": "PREPAID",
        "card_bin_id": 1001,
        "recharge_amount": Decimal("100"),
    }
    payload.update(overrides)
    return payload


def budget(**overrides):
    payload = {
        "request_no": "kaika_202606250002ab",
        "card_type": "BUDGET",
        "card_bin_id": 1001,
        "card_group_id": 12,
        "budget_id": 9001,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "request_no",
    [
        "short1",
        "1abcdefgh",
        "abcdefgh",
        "abc defgh1",
        "a" * 65,
        "kaika_中文123",
    ],
)
def test_request_no_rejects_bad_values(request_no):
    with pytest.raises(ValidationError):
        CardApplyRequest(**prepaid(request_no=request_no))


@pytest.mark.parametrize("request_no", ["kaika_123", "kaika_202606250001ab", "A1-b_c_d_e"])
def test_request_no_accepts_valid_values(request_no):
    assert CardApplyRequest(**prepaid(request_no=request_no)).request_no == request_no


def test_prepaid_requires_recharge_amount():
    with pytest.raises(ValidationError):
        CardApplyRequest(**prepaid(recharge_amount=None))


def test_prepaid_rejects_budget_fields():
    with pytest.raises(ValidationError):
        CardApplyRequest(**prepaid(card_group_id=12))
    with pytest.raises(ValidationError):
        CardApplyRequest(**prepaid(limit_config=LimitConfig()))


def test_budget_requires_group_and_budget():
    with pytest.raises(ValidationError):
        CardApplyRequest(**budget(card_group_id=None))
    with pytest.raises(ValidationError):
        CardApplyRequest(**budget(budget_id=None))


def test_budget_rejects_recharge_amount():
    with pytest.raises(ValidationError):
        CardApplyRequest(**budget(recharge_amount=Decimal("10.00")))


def test_money_serializes_as_two_decimals():
    dumped = CardApplyRequest(**prepaid(recharge_amount=Decimal("100"))).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    assert dumped["rechargeAmount"] == "100.00"


def test_amount_with_three_decimals_is_rejected():
    with pytest.raises(ValidationError):
        CardApplyRequest(**prepaid(recharge_amount=Decimal("1.234")))


def test_negative_limit_is_rejected():
    with pytest.raises(ValidationError):
        LimitConfig(day_limit=Decimal("-1.00"))


def test_only_one_period_limit_allowed():
    with pytest.raises(ValidationError):
        LimitConfig(day_limit=Decimal("100.00"), month_limit=Decimal("500.00"))
    config = LimitConfig(day_limit=Decimal("100.00"), per_trans_limit=Decimal("50.00"))
    assert config.day_limit == Decimal("100.00")


def test_convert_rejects_same_currency_and_bad_amount():
    with pytest.raises(ValidationError):
        ConvertRequest(from_currency="USD", to_currency="USD", amount=Decimal("1.00"))
    with pytest.raises(ValidationError):
        ConvertRequest(from_currency="USD", to_currency="USDT", amount=Decimal("0"))
    with pytest.raises(ValidationError):
        ConvertRequest(from_currency="USD", to_currency="USDT", amount=Decimal("-1.00"))
    with pytest.raises(ValidationError):
        ConvertRequest(from_currency="USD", to_currency="USDT", amount=Decimal("1.001"))


def test_convert_dumps_camel_case_string_amount():
    request = ConvertRequest(from_currency="USDT", to_currency="USD", amount="99.5")
    assert request.model_dump(mode="json", by_alias=True) == {
        "fromCurrency": "USDT",
        "toCurrency": "USD",
        "amount": "99.50",
    }


def test_apply_result_ignores_new_fields_and_flags_pending_funding():
    result = CardApplyResult.model_validate(
        {
            "taskId": 901,
            "batchNo": "BATCH1",
            "requestNo": "kaika_202606250001ab",
            "applyStatus": "SUCCESS",
            "initialFundingStatus": "PENDING",
            "initialFundingPendingAmount": 100,
            "cardIds": ["VC1"],
            "somethingNewFromPlatform": "ignored",
        }
    )
    assert result.is_success
    assert result.initial_funding_pending
    assert result.initial_funding_pending_amount == Decimal("100")


def test_card_bin_tolerates_malformed_supported_countries():
    result = CardBinResult.model_validate(
        {
            "quota": {"maxCardCount": 1, "issuedCardCount": 0, "remainingCardCount": 1},
            "bins": [
                {
                    "binId": 1,
                    "bin": "486880",
                    "cardOrg": "VISA",
                    "cardType": "PREPAID",
                    "currency": "USD",
                    "supportedCountries": "not-json",
                }
            ],
        }
    )
    assert result.bins[0].supported_countries == []
