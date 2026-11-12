"""BYOC 异步客户端（httpx 版）。

逻辑与上游同步版逐行对齐：预算/退避/同 request_id 恢复都不变，只把 requests 换成
httpx.AsyncClient、time.sleep 换成可注入的 asyncio.sleep。POST 不做透明重试，
httpx 也不配 retries、不跟随重定向。
"""

from __future__ import annotations

import asyncio
import math
import posixpath
import random
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, unquote, urlsplit

import httpx

from app.integrations.byoc.errors import (
    APIError,
    OrderValidationError,
    OutcomeUnknown,
    PollTimeout,
)
from app.integrations.ratelimit import RateLimiter


class _BudgetExceeded(Exception):
    pass


class _InvalidResponse(Exception):
    pass


@dataclass(frozen=True)
class OrderValidation:
    request_id: str
    valid: bool
    quote_bound: bool | None = None
    accepting_orders: bool | None = None
    would_create_new_order: bool | None = None
    idempotent_replay: bool | None = None
    order: dict | None = None


@dataclass(frozen=True)
class Quote:
    quote_id: str
    plan_type: str
    billing_country: str | None
    authorized_amount_minor: int
    authorized_currency: str
    display_amount: str
    valid_until: datetime
    pricing_version: str | None = None


class ByocClient:
    ORDER_PATHS = frozenset(
        {"/orders", "/codex-credits/orders", "/upgrades/orders", "/half-price/orders"}
    )

    def __init__(
        self,
        key: str,
        base_url: str = "https://wangbohui.com",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=None,
        clock=time.monotonic,
        jitter=None,
        limiter: RateLimiter | None = None,
    ) -> None:
        if not key:
            raise ValueError("BYOC API Key 不能为空")
        address = urlsplit(base_url)
        if (
            address.scheme != "https"
            or not address.hostname
            or address.username
            or address.password
            or address.query
            or address.fragment
            or address.path.rstrip("/") not in {"", "/api/v1/external"}
        ):
            raise ValueError("base_url 必须是本站 HTTPS 地址或 /api/v1/external 地址")
        self.base = base_url.rstrip("/")
        if not address.path.rstrip("/"):
            self.base += "/api/v1/external"
        self.http = httpx.AsyncClient(
            base_url=self.base,
            headers={
                "X-API-Key": key,
                "Accept": "application/json",
                "User-Agent": "ByocClient/1.12-async",
            },
            follow_redirects=False,
            transport=transport,
        )
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._clock = clock
        self._jitter = jitter if jitter is not None else lambda: random.uniform(0, 0.5)
        self._limiter = limiter if limiter is not None else RateLimiter("byoc", 300)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> ByocClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @classmethod
    def from_settings(cls, settings=None, *, limiter: RateLimiter | None = None) -> ByocClient:
        from app.config import get_settings

        settings = settings or get_settings()
        if not settings.byoc_configured:
            raise ValueError("未配置 BYOC_API_KEY")
        limiter = limiter or RateLimiter("byoc", settings.byoc_rate_limit_per_minute)
        return cls(
            settings.byoc_api_key.get_secret_value(),
            settings.byoc_base_url,
            limiter=limiter,
        )

    # ------------------------------------------------------------------ 预算与退避

    def _deadline(self, timeout):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout 必须是正数秒")
        return self._clock() + timeout

    def _remaining(self, deadline):
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _BudgetExceeded
        return remaining

    async def _pause(self, delay, deadline):
        if delay >= self._remaining(deadline):
            raise _BudgetExceeded
        await self._sleep(delay)

    def _delay(self, error, failures):
        delay = min(15, 2 ** min(max(0, failures - 1), 4))
        if isinstance(error, APIError):
            delay = max(
                delay, error.retry_after_seconds(default=15 if error.status == 503 else delay)
            )
        return delay + max(0, min(0.5, float(self._jitter())))

    @staticmethod
    def _transient(error):
        return error.status == 429 or 500 <= error.status < 600

    # ------------------------------------------------------------------ HTTP

    async def _call(self, method, path, deadline, payload=None):
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or urlsplit(path).netloc
            or urlsplit(path).fragment
        ):
            raise ValueError("API 路径只能是本站相对路径")
        # 每次真实请求（含重试和 recover）都先过桶
        await self._limiter.acquire()
        remaining = self._remaining(deadline)
        timeout = httpx.Timeout(
            connect=min(10, remaining / 2),
            read=min(60, remaining / 2),
            write=min(60, remaining / 2),
            pool=min(10, remaining / 2),
        )
        options = {"timeout": timeout, "headers": {"X-Request-ID": uuid.uuid4().hex}}
        if payload is not None:
            options["json"] = payload
        response = await self.http.request(method, self.base + path, **options)
        is_json = "json" in response.headers.get("Content-Type", "").lower()
        try:
            data = response.json()
        except ValueError:
            data, is_json = {}, False
        if not isinstance(data, dict):
            data, is_json = {}, False
        if not 200 <= response.status_code < 300:
            raise APIError(
                response.status_code,
                data.get("detail", "响应无法确认"),
                retry_after=response.headers.get("Retry-After"),
                trace_id=response.headers.get("X-Request-ID"),
                response_is_json=is_json,
            )
        if not is_json or data.get("ok") is not True:
            raise _InvalidResponse
        return data

    @staticmethod
    def _order(data, *, request_id=None, task_id=None):
        order = data.get("order")
        if (
            not isinstance(order, dict)
            or not isinstance(order.get("task_id"), str)
            or not order["task_id"]
            or (request_id is not None and order.get("request_id") != request_id)
            or (task_id is not None and order["task_id"] != task_id.strip().upper())
        ):
            raise _InvalidResponse
        state, settlement = order.get("payment_status"), order.get("settlement")
        result = order.get("recharge_result")
        if (
            state
            not in {
                "queued",
                "processing",
                "requires_action",
                "manual_review",
                "completed",
                "failed",
                "cancelled",
            }
            or settlement not in {"NONE", "NO_CHARGE", "UNKNOWN", "PAID"}
            or result not in {"pending", "uncertain", "success", "failed"}
            or any(
                type(order.get(name)) is not bool
                for name in ("payment_success", "polling_required")
            )
        ):
            raise _InvalidResponse
        success = state == "completed" and settlement == "PAID"
        if (
            order["payment_success"] != success
            or (result == "success") != success
            or (state == "completed" and not success)
            or (state in {"failed", "cancelled"} and result != "failed")
            or (state == "requires_action" and result != "uncertain")
            or (result == "pending" and state not in {"queued", "processing"})
            or (settlement == "UNKNOWN" and result != "uncertain")
        ):
            raise _InvalidResponse
        return order

    # ------------------------------------------------------------------ 报价 / 校验

    async def quote(self, plan_type, billing_country=None, *, timeout=30):
        path = "/quotes/" + quote(plan_type, safe="")
        if billing_country:
            path += "?billing_country=" + quote(billing_country.lower(), safe="")
        try:
            data = await self._read(path, self._deadline(timeout))
            return self._quote(data, plan_type=plan_type, billing_country=billing_country)
        except (_BudgetExceeded, _InvalidResponse):
            raise PollTimeout(path) from None

    @staticmethod
    def _quote(data, *, plan_type, billing_country) -> Quote:
        quote_id = data.get("quote_id")
        returned_plan = data.get("plan_type")
        amount = data.get("authorized_amount_minor")
        currency = data.get("authorized_currency")
        display = data.get("display_amount")
        country = data.get("billing_country")
        valid_until = data.get("valid_until")
        if (
            not isinstance(quote_id, str)
            or not quote_id
            or not isinstance(returned_plan, str)
            or returned_plan.lower() != plan_type.lower()
            or isinstance(amount, bool)
            or not isinstance(amount, int)
            or amount <= 0
            or not isinstance(currency, str)
            or not currency
            or not isinstance(display, str)
            or not display
            or not isinstance(country, str)
            or (billing_country and country.lower() != billing_country.lower())
            or not isinstance(valid_until, str)
        ):
            raise _InvalidResponse
        try:
            expires = datetime.fromisoformat(valid_until)
        except ValueError as exc:
            raise _InvalidResponse from exc
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return Quote(
            quote_id=quote_id,
            plan_type=returned_plan,
            billing_country=country.lower(),
            authorized_amount_minor=amount,
            authorized_currency=currency,
            display_amount=display,
            valid_until=expires,
            pricing_version=data.get("pricing_version"),
        )

    async def validate_order(self, order, *, timeout=60) -> OrderValidation:
        """校验订单。真实接口在“还没建单”时返回 valid/quote_bound，没有 order 字段。"""
        if not isinstance(order, dict):
            raise ValueError("order 必须是 dict")
        request_id = order.get("request_id")
        try:
            data = await self._call(
                "POST", "/orders/validate", self._deadline(timeout), deepcopy(order)
            )
        except _BudgetExceeded:
            raise PollTimeout("/orders/validate") from None
        except _InvalidResponse:
            raise OrderValidationError("响应不是 ok=true") from None

        if not isinstance(data, dict) or data.get("ok") is not True:
            raise OrderValidationError("响应不是 ok=true", detail=data)

        if "order" in data:
            parsed = self._order(data, request_id=request_id)
            return OrderValidation(
                request_id=request_id or "",
                valid=True,
                idempotent_replay=data.get("idempotent_replay"),
                order=parsed,
            )

        returned_request_id = data.get("request_id")
        if returned_request_id != request_id:
            raise OrderValidationError(
                f"request_id 不匹配（返回 {returned_request_id}）",
                detail=data,
            )
        plan_type = data.get("plan_type")
        expected_plan = order.get("plan_type")
        if isinstance(plan_type, str) and isinstance(expected_plan, str):
            if plan_type.lower() != expected_plan.lower():
                raise OrderValidationError(
                    f"套餐不匹配（返回 {plan_type}，请求 {expected_plan}）",
                    detail=data,
                )
        if data.get("valid") is not True:
            raise OrderValidationError("valid != true", detail=data)
        for flag in ("quote_bound", "accepting_orders"):
            if data.get(flag) is False:
                raise OrderValidationError(f"{flag}=false", detail=data)

        return OrderValidation(
            request_id=returned_request_id or "",
            valid=True,
            quote_bound=data.get("quote_bound"),
            accepting_orders=data.get("accepting_orders"),
            would_create_new_order=data.get("would_create_new_order"),
            idempotent_replay=data.get("idempotent_replay"),
            order=None,
        )

    async def create_order(self, order, *, timeout=120):
        """先 validate 再建单；两步用同一份 JSON、同一个 request_id。"""
        await self.validate_order(order, timeout=min(timeout, 60))
        return await self.create("/orders", order, timeout=timeout)

    # ------------------------------------------------------------------ 建单与恢复

    async def _recover(self, request_id, deadline):
        failures, last_error = 0, None
        while True:
            try:
                data = await self._call(
                    "GET", "/orders/by-request/" + quote(request_id, safe=""), deadline
                )
                return self._order(data, request_id=request_id)
            except APIError as error:
                if error.status == 404 and error.response_is_json:
                    return None
                if not self._transient(error):
                    raise
                last_error = error
            except (httpx.HTTPError, _InvalidResponse):
                pass
            failures += 1
            try:
                await self._pause(self._delay(last_error, failures), deadline)
            except _BudgetExceeded:
                raise OutcomeUnknown(request_id, last_error=last_error) from None

    async def call(self, method, path, payload=None, *, timeout=60):
        """单次非建单调用，例如报价；建单请走 create/create_order。"""
        method = method.upper()
        if (
            method == "POST"
            and posixpath.normpath(unquote(urlsplit(path).path)) in self.ORDER_PATHS
        ):
            raise ValueError("建单请使用 create()，以保留原请求号和恢复流程")
        try:
            return await self._call(method, path, self._deadline(timeout), payload)
        except (_BudgetExceeded, _InvalidResponse):
            raise PollTimeout(path) from None

    async def recover(self, request_id, *, timeout=120):
        """按原请求号找回订单；None 只表示暂时没找到。"""
        try:
            return await self._recover(request_id, self._deadline(timeout))
        except _BudgetExceeded:
            raise OutcomeUnknown(request_id) from None

    async def create(self, path, order, *, timeout=120, max_attempts=3):
        if path not in self.ORDER_PATHS:
            raise ValueError("不支持的建单路径")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须是 1–10 的整数")
        request_id = order.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{7,95}", request_id
        ):
            raise ValueError("先为业务订单生成并保存固定 request_id")
        deadline = self._deadline(timeout)
        payload = deepcopy(order)
        last_error = None
        prior_ambiguous = False
        try:
            for attempt in range(1, max_attempts + 1):
                post_error, waited = None, False
                try:
                    return self._order(
                        await self._call("POST", path, deadline, payload),
                        request_id=request_id,
                    )
                except APIError as error:
                    last_error = error
                    post_error = error
                    if error.status == 409 and error.response_is_json:
                        try:
                            recovered = await self._recover(request_id, deadline)
                            if recovered:
                                error.recovered_task_id = recovered["task_id"]
                        except (APIError, OutcomeUnknown, _BudgetExceeded):
                            pass
                        if prior_ambiguous and error.recovered_task_id is None:
                            raise OutcomeUnknown(request_id, last_error=error) from None
                        raise error from None
                    if (
                        error.status in {400, 401, 402, 403, 404, 413, 422}
                        and error.response_is_json
                    ):
                        if prior_ambiguous:
                            try:
                                recovered = await self._recover(request_id, deadline)
                                if recovered is not None:
                                    return recovered
                            except (APIError, OutcomeUnknown, _BudgetExceeded):
                                pass
                            raise OutcomeUnknown(request_id, last_error=error) from None
                        raise
                    prior_ambiguous = True
                    if error.status in {429, 503}:
                        await self._pause(self._delay(error, attempt), deadline)
                        waited = True
                except (httpx.HTTPError, _InvalidResponse):
                    prior_ambiguous = True
                try:
                    recovered = await self._recover(request_id, deadline)
                except APIError as error:
                    if prior_ambiguous:
                        raise OutcomeUnknown(request_id, last_error=error) from None
                    raise
                if recovered is not None:
                    return recovered
                if attempt < max_attempts and not waited:
                    await self._pause(self._delay(post_error, attempt), deadline)
        except _BudgetExceeded:
            pass
        finally:
            payload.clear()
        raise OutcomeUnknown(request_id, last_error=last_error) from None

    # ------------------------------------------------------------------ 查询

    async def _read(self, path, deadline):
        failures = 0
        while True:
            error = None
            try:
                return await self._call("GET", path, deadline)
            except APIError as failure:
                if not self._transient(failure):
                    raise
                error = failure
            except (httpx.HTTPError, _InvalidResponse):
                pass
            failures += 1
            await self._pause(self._delay(error, failures), deadline)

    async def status(self, task_id, *, timeout=60):
        try:
            data = await self._read("/orders/" + quote(task_id, safe=""), self._deadline(timeout))
            return self._order(data, task_id=task_id)
        except (_BudgetExceeded, _InvalidResponse):
            raise PollTimeout(task_id) from None

    async def wait(self, task_id, *, timeout=600, interval=10):
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("interval 必须是正数秒")
        deadline = self._deadline(timeout)
        try:
            while True:
                order = self._order(
                    await self._read("/orders/" + quote(task_id, safe=""), deadline),
                    task_id=task_id,
                )
                if (
                    order["payment_success"]
                    or not order["polling_required"]
                    or order.get("requires_user_action") is True
                ):
                    return order
                await self._pause(max(5, interval), deadline)
        except (_BudgetExceeded, _InvalidResponse):
            raise PollTimeout(task_id) from None

    async def invoice(self, task_id, *, timeout=60):
        try:
            data = await self._read(
                "/orders/" + quote(task_id, safe="") + "/invoice", self._deadline(timeout)
            )
            invoice = data.get("invoice")
            if (
                data.get("task_id") != task_id.strip().upper()
                or not isinstance(invoice, dict)
                or invoice.get("state")
                not in {"pending", "running", "ready", "failed", "unavailable", "expired"}
            ):
                raise _InvalidResponse
            return invoice
        except (_BudgetExceeded, _InvalidResponse):
            raise PollTimeout(task_id) from None

    async def invoice_link(self, task_id, *, wait=False, timeout=120):
        deadline = self._deadline(timeout)
        path = "/orders/" + quote(task_id, safe="") + "/invoice/download"
        try:
            while True:
                try:
                    data = await self._read(path, deadline)
                    address = urlsplit(data.get("download_url") or "")
                    if (
                        data.get("task_id") != task_id.strip().upper()
                        or address.scheme != "https"
                        or address.username
                        or address.password
                        or address.port not in {None, 443}
                        or not (
                            (
                                address.hostname == "pay.stripe.com"
                                and address.path.startswith("/invoice/")
                            )
                            or (
                                address.hostname == "invoice.stripe.com"
                                and address.path.startswith("/i/")
                            )
                        )
                        or not isinstance(data.get("expires_at"), str)
                        or not data["expires_at"]
                    ):
                        raise _InvalidResponse
                    return data
                except APIError as error:
                    if not (
                        wait
                        and error.status == 409
                        and error.code == "INVOICE_NOT_READY"
                        and error.retry_after
                    ):
                        raise
                    await self._pause(max(1, error.retry_after_seconds(default=5)), deadline)
        except (_BudgetExceeded, _InvalidResponse, ValueError):
            raise PollTimeout(task_id) from None
