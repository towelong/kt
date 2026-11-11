"""AiLink VCC 开放 API 客户端。

只用异步 httpx：FastAPI 里直接 await，复用连接池；脚本里用 `asyncio.run(...)`。
资金操作不自动重试——500/超时都代表“结果未知”，换 requestNo 重提可能重复扣款。
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal, Self

import httpx

from app.config import Settings, get_settings
from app.integrations.ailink.errors import (
    AiLinkAuthError,
    AiLinkBusinessError,
    AiLinkConfigError,
    AiLinkError,
    AiLinkNotFoundError,
    AiLinkPermissionError,
    AiLinkProtocolError,
    AiLinkRateLimitError,
    AiLinkServerError,
    AiLinkTransportError,
)
from app.integrations.ailink.models import (
    AccountBalanceResult,
    ApiEnvelope,
    BalanceConvertResult,
    CardApplyRequest,
    CardApplyResult,
    CardBinResult,
    ConvertRequest,
    validate_payload,
)
from app.integrations.ailink.signing import request_signature

logger = logging.getLogger(__name__)

PATH_CARD_BINS = "/openapi/v1/card-bins/query"
PATH_ACCOUNT_BALANCE = "/openapi/v1/account/balance/query"
PATH_ACCOUNT_CONVERT = "/openapi/v1/account/convert"
PATH_CARDS_APPLY = "/openapi/v1/cards/apply"

_ERROR_BY_STATUS: dict[int, type[AiLinkError]] = {
    400: AiLinkBusinessError,
    401: AiLinkAuthError,
    403: AiLinkPermissionError,
    404: AiLinkNotFoundError,
    429: AiLinkRateLimitError,
    500: AiLinkServerError,
}


class AiLinkClient:
    """一个商户一套 Key/Secret；不同商户就建多个实例。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not api_key or not api_secret:
            raise AiLinkConfigError("AiLinkClient 需要 base_url、api_key、api_secret")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            transport=transport,
            headers={"User-Agent": "kt-ailink/0.1 (+python-httpx)"},
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Self:
        settings = settings or get_settings()
        if settings.ailink_api_key is None or settings.ailink_api_secret is None:
            raise AiLinkConfigError("未配置 AILINK_API_KEY / AILINK_API_SECRET，无法调用 VCC")
        return cls(
            base_url=settings.ailink_base_url,
            api_key=settings.ailink_api_key.get_secret_value(),
            api_secret=settings.ailink_api_secret.get_secret_value(),
            timeout=settings.ailink_timeout_seconds,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 业务接口

    async def list_card_bins(self, *, request_id: str | None = None) -> CardBinResult:
        """获取卡 BIN；开卡配额看 quota，不要看单条 BIN 上的兼容字段。"""
        data = await self._post(PATH_CARD_BINS, {}, request_id=request_id)
        return validate_payload(
            CardBinResult,
            {"quota": data.get("quota"), "bins": data.get("list") or []},
            "获取卡 BIN",
        )

    async def get_account_balance(self, *, request_id: str | None = None) -> AccountBalanceResult:
        """主账户 USD / USDT 余额，含冻结金额和可用余额。"""
        data = await self._post(PATH_ACCOUNT_BALANCE, {}, request_id=request_id)
        return validate_payload(AccountBalanceResult, data, "获取账户余额")

    async def convert_balance(
        self,
        from_currency: Literal["USD", "USDT"],
        to_currency: Literal["USD", "USDT"],
        amount: Decimal | str | int | float,
        *,
        request_id: str | None = None,
    ) -> BalanceConvertResult:
        """账户余额兑换，USD→USDT 不收费，USDT→USD 按商户费率。"""
        payload = ConvertRequest(
            from_currency=from_currency,
            to_currency=to_currency,
            amount=amount,
        )
        data = await self._post(
            PATH_ACCOUNT_CONVERT,
            payload.model_dump(mode="json", by_alias=True, exclude_none=True),
            request_id=request_id,
        )
        return validate_payload(BalanceConvertResult, data, "账户余额兑换")

    async def apply_cards(
        self,
        request: CardApplyRequest,
        *,
        request_id: str | None = None,
    ) -> CardApplyResult:
        """申请卡。同步 SUCCESS + initialFundingStatus=PENDING 很常见，首充结果等 Webhook。"""
        data = await self._post(
            PATH_CARDS_APPLY,
            request.model_dump(mode="json", by_alias=True, exclude_none=True),
            request_id=request_id,
        )
        return validate_payload(CardApplyResult, data, "申请卡")

    # ------------------------------------------------------------------ 内部实现

    async def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        request_id = request_id or f"kt_{uuid.uuid4().hex}"
        headers = {
            "Content-Type": "application/json",
            "X-VCC-API-KEY": self._api_key,
            "X-VCC-TIMESTAMP": timestamp,
            "X-VCC-NONCE": nonce,
            "X-VCC-SIGNATURE": request_signature(
                self._api_secret,
                method="POST",
                path=path,
                timestamp_ms=timestamp,
                nonce=nonce,
                body=body,
            ),
            "X-VCC-REQUEST-ID": request_id,
        }

        logger.debug("VCC 请求 POST %s requestId=%s", path, request_id)
        try:
            response = await self._client.post(path, content=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise AiLinkTransportError(
                "请求 AiLink VCC 超时，结果未知；用原 requestNo/requestId 查询核对，不要换号重提",
                request_id=request_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise AiLinkTransportError(
                f"请求 AiLink VCC 失败：{type(exc).__name__}",
                request_id=request_id,
            ) from exc

        logger.debug(
            "VCC 响应 POST %s status=%s requestId=%s",
            path,
            response.status_code,
            request_id,
        )
        return self._parse_response(response, request_id=request_id)

    def _parse_response(
        self,
        response: httpx.Response,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        envelope: ApiEnvelope | None = None
        try:
            raw: Any = response.json()
        except ValueError:
            raw = None
        if isinstance(raw, dict):
            try:
                envelope = ApiEnvelope.model_validate(raw)
            except ValueError:
                envelope = None

        resp_request_id = envelope.request_id if envelope else None
        code = envelope.code if envelope else None
        message = envelope.msg if envelope else ""

        if response.status_code == 200 and code == 200 and envelope is not None:
            return envelope.data or {}

        error_cls: type[AiLinkError] | None = None
        if response.status_code != 200:
            error_cls = _ERROR_BY_STATUS.get(response.status_code)
            if error_cls is None:
                error_cls = AiLinkServerError if response.status_code >= 500 else AiLinkError
        if error_cls is None and code is not None:
            # HTTP 200 但业务 code 非 200：文档允许这种“受理失败”，按业务错误翻译
            error_cls = _ERROR_BY_STATUS.get(code)
            if error_cls is None:
                error_cls = AiLinkServerError if code >= 500 else AiLinkBusinessError
        if error_cls is None:
            error_cls = AiLinkProtocolError if response.status_code == 200 else AiLinkError
        if not message and envelope is None:
            message = f"响应不是合法 JSON（HTTP {response.status_code}）"
        raise error_cls(
            f"AiLink VCC 返回错误 HTTP {response.status_code}"
            f"{f' / code {code}' if code is not None else ''}：{message or '无错误信息'}",
            request_id=resp_request_id or request_id,
            status_code=response.status_code,
            code=code,
        )


def new_request_no(prefix: str = "kaika_") -> str:
    """生成符合 VCC 规则的 requestNo：字母开头、含字母和数字、8-64 位、仅 _ 和 -。"""
    safe_prefix = "".join(char for char in prefix if char.isalnum() or char in "_-") or "req_"
    if not safe_prefix[0].isalpha():
        safe_prefix = f"req_{safe_prefix}"
    # 时间戳保证递增，随机串保证同一毫秒内不撞号；两者都带数字，前缀带字母
    candidate = f"{safe_prefix}{int(time.time() * 1000)}{secrets.token_hex(4)}"
    if not any(char.isalpha() for char in candidate):
        candidate = f"{candidate}a"
    return candidate[:64]
