"""客户端测试：走 httpx.MockTransport，不碰网络。

每个用例都在 handler 里重算一次签名，等于用“服务端视角”校验客户端的请求。
"""

from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.integrations.ailink.client import AiLinkClient, new_request_no
from app.integrations.ailink.errors import (
    AiLinkAuthError,
    AiLinkBusinessError,
    AiLinkConfigError,
    AiLinkNotFoundError,
    AiLinkPermissionError,
    AiLinkProtocolError,
    AiLinkRateLimitError,
    AiLinkServerError,
    AiLinkTransportError,
)
from app.integrations.ailink.models import CardApplyRequest
from app.integrations.ailink.signing import request_signature

API_SECRET = "sk_test_secret"


class RecordingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self, *, max_wait=None) -> None:
        self.calls += 1


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(handler: Any, limiter=None, sleep=None) -> AiLinkClient:
    return AiLinkClient(
        base_url="https://card.example.test",
        api_key="ak_test",
        api_secret=API_SECRET,
        transport=httpx.MockTransport(handler),
        limiter=limiter,
        sleep=sleep,
    )


def assert_signed(request: httpx.Request) -> None:
    assert request.headers["X-VCC-API-KEY"] == "ak_test"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-VCC-REQUEST-ID"]
    expected = request_signature(
        API_SECRET,
        method=request.method,
        path=request.url.path,
        timestamp_ms=request.headers["X-VCC-TIMESTAMP"],
        nonce=request.headers["X-VCC-NONCE"],
        body=request.content,
    )
    assert request.headers["X-VCC-SIGNATURE"] == expected


CARD_BINS_RESPONSE = {
    "code": 200,
    "msg": "操作成功",
    "requestId": "req_bins",
    "data": {
        "quota": {"maxCardCount": 100, "issuedCardCount": 3, "remainingCardCount": 97},
        "list": [
            {
                "binId": 1001,
                "bin": "486880",
                "cardOrg": "VISA",
                "cardType": "PREPAID",
                "currency": "USD",
                "issuingCountry": "US",
                "supportedCountries": '["US","SG"]',
                "maxOpenCardCount": 1,
                "minOpenCardAmount": "10.00",
                "minRechargeAmount": "10.00",
                "maxCardCount": 100,
                "issuedCardCount": 3,
            }
        ],
    },
}

BALANCE_RESPONSE = {
    "code": 200,
    "msg": "操作成功",
    "requestId": "req_balance",
    "data": {
        "balances": [
            {
                "currency": "USD",
                "balance": "100.50",
                "frozenAmount": "1.25",
                "availableBalance": "99.25",
            },
            {
                "currency": "USDT",
                "balance": "0.00",
                "frozenAmount": "0.00",
                "availableBalance": "0.00",
            },
        ]
    },
}

CONVERT_RESPONSE = {
    "code": 200,
    "msg": "操作成功",
    "requestId": "req_convert",
    "data": {
        "orderNo": "AC202606250001",
        "fromCurrency": "USDT",
        "toCurrency": "USD",
        "amount": "100.00",
        "feeRate": "0.010000",
        "feeAmount": "1.00",
        "receivedAmount": "99.00",
    },
}

APPLY_RESPONSE = {
    "code": 200,
    "msg": "操作成功",
    "requestId": "req_apply",
    "data": {
        "taskId": 901,
        "batchNo": "BATCH202606250001",
        "requestNo": "kaika_202606250001ab",
        "applyStatus": "SUCCESS",
        "applyStatusText": "成功",
        "taskStatus": "SUCCESS",
        "taskStatusText": "成功",
        "deliveryMode": "POOL_IMMEDIATE",
        "initialFundingStatus": "PENDING",
        "initialFundingPendingCount": 1,
        "initialFundingFailedCount": 0,
        "initialFundingPendingAmount": 100,
        "cardIds": ["VC202606250001"],
    },
}


async def test_list_card_bins_parses_quota_and_countries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/openapi/v1/card-bins/query"
        assert request.content == b"{}"
        assert_signed(request)
        return httpx.Response(200, json=CARD_BINS_RESPONSE)

    async with make_client(handler) as client:
        result = await client.list_card_bins()

    assert result.quota.max_card_count == 100
    assert result.quota.remaining_card_count == 97
    assert result.bins[0].bin == "486880"
    assert result.bins[0].supported_countries == ["US", "SG"]
    assert result.bins[0].min_open_card_amount == Decimal("10.00")


async def test_account_balance_keeps_decimal_precision():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openapi/v1/account/balance/query"
        assert_signed(request)
        return httpx.Response(200, json=BALANCE_RESPONSE)

    async with make_client(handler) as client:
        result = await client.get_account_balance()

    usd = result.get("usd")
    assert usd is not None
    assert usd.balance == Decimal("100.50")
    assert usd.available_balance == Decimal("99.25")


async def test_convert_balance_sends_string_amount_and_camel_case():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        assert_signed(request)
        return httpx.Response(200, json=CONVERT_RESPONSE)

    async with make_client(handler) as client:
        result = await client.convert_balance("USDT", "USD", Decimal("100"))

    assert captured["body"] == (b'{"fromCurrency":"USDT","toCurrency":"USD","amount":"100.00"}')
    assert result.fee_amount == Decimal("1.00")
    assert result.received_amount == Decimal("99.00")
    assert result.fee_rate == Decimal("0.010000")


async def test_apply_cards_serializes_only_sent_fields():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        assert_signed(request)
        return httpx.Response(200, json=APPLY_RESPONSE)

    request = CardApplyRequest(
        request_no="kaika_202606250001ab",
        card_type="PREPAID",
        card_bin_id=1001,
        recharge_amount=Decimal("100"),
    )
    async with make_client(handler) as client:
        result = await client.apply_cards(request)

    assert captured["body"] == (
        b'{"requestNo":"kaika_202606250001ab","cardType":"PREPAID",'
        b'"cardBinId":1001,"cardCount":1,"rechargeAmount":"100.00"}'
    )
    assert result.is_success is True
    assert result.initial_funding_pending is True
    assert result.card_ids == ["VC202606250001"]


async def test_request_id_can_be_provided():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-VCC-REQUEST-ID"] == "order-42"
        return httpx.Response(200, json=BALANCE_RESPONSE)

    async with make_client(handler) as client:
        await client.get_account_balance(request_id="order-42")


@pytest.mark.parametrize(
    ("status_code", "error_cls"),
    [
        (400, AiLinkBusinessError),
        (401, AiLinkAuthError),
        (403, AiLinkPermissionError),
        (404, AiLinkNotFoundError),
        (429, AiLinkRateLimitError),
        (500, AiLinkServerError),
        (502, AiLinkServerError),
    ],
)
async def test_http_errors_map_to_typed_exceptions(status_code, error_cls):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"code": status_code, "msg": "余额不足", "requestId": "req_err"},
        )

    async with make_client(handler) as client:
        with pytest.raises(error_cls) as info:
            await client.get_account_balance()

    assert info.value.request_id == "req_err"
    assert info.value.status_code == status_code
    assert "余额不足" in str(info.value)


async def test_envelope_code_with_http_200_is_business_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 400, "msg": "卡状态不允许操作", "requestId": "req_x"}
        )

    async with make_client(handler) as client:
        with pytest.raises(AiLinkBusinessError) as info:
            await client.convert_balance("USD", "USDT", Decimal("1.00"))

    assert info.value.request_id == "req_x"


async def test_read_endpoint_retries_429_once():
    requests: list[int] = []
    limiter = RecordingLimiter()
    sleep = RecordingSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        if len(requests) == 1:
            return httpx.Response(
                429,
                json={"code": 429, "msg": "too many requests", "requestId": "req_429"},
                headers={"Retry-After": "0"},
            )
        return httpx.Response(200, json=BALANCE_RESPONSE)

    async with make_client(handler, limiter=limiter, sleep=sleep) as client:
        result = await client.get_account_balance()

    assert result.get("USD") is not None
    assert len(requests) == 2
    assert limiter.calls == 2
    assert sleep.calls == [0.0]


async def test_fund_operation_does_not_retry_on_429():
    requests: list[int] = []
    limiter = RecordingLimiter()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(
            429,
            json={"code": 429, "msg": "too many requests", "requestId": "req_429"},
            headers={"Retry-After": "0"},
        )

    request = CardApplyRequest(
        request_no="kaika_202606250001ab",
        card_type="PREPAID",
        card_bin_id=1001,
        recharge_amount=Decimal("100"),
    )
    async with make_client(handler, limiter=limiter) as client:
        with pytest.raises(AiLinkRateLimitError):
            await client.apply_cards(request)

    assert len(requests) == 1
    assert limiter.calls == 1


async def test_non_json_success_response_is_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    async with make_client(handler) as client:
        with pytest.raises(AiLinkProtocolError):
            await client.get_account_balance()


async def test_timeout_is_transport_error_with_request_id():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with make_client(handler) as client:
        with pytest.raises(AiLinkTransportError) as info:
            await client.get_account_balance(request_id="timeout-1")

    assert info.value.request_id == "timeout-1"
    assert "结果未知" in str(info.value)


def test_from_settings_requires_credentials():
    settings = Settings(_env_file=None)
    with pytest.raises(AiLinkConfigError):
        AiLinkClient.from_settings(settings)


def test_from_settings_reads_secrets():
    settings = Settings(
        _env_file=None,
        ailink_api_key="ak_from_settings",
        ailink_api_secret="sk_from_settings",
        ailink_base_url="https://card.example.test",
    )
    client = AiLinkClient.from_settings(settings)
    assert client.base_url == "https://card.example.test"
    assert client._api_key == "ak_from_settings"


@pytest.mark.parametrize("prefix", ["kaika_", "req_", "HOLDER", "1bad", ""])
def test_new_request_no_matches_platform_rules(prefix):
    numbers = {new_request_no(prefix) for _ in range(50)}
    assert len(numbers) == 50
    for number in numbers:
        assert 8 <= len(number) <= 64
        assert number[0].isalpha()
        assert any(char.isdigit() for char in number)
        assert all(char.isalnum() or char in "_-" for char in number)
