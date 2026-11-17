"""订阅任务：BYOC 报价 → 校验 → 下单，结果通过回调返回。

设计约束：
- session 只在任务 payload 密文里，建单成功或进入终态后清空（失败重试期间保留）；
- 卡在结果确定前锁定，失败释放，未知保留等 recover；
- 成功/失败只认 BYOC 的 order 状态，不靠 HTTP 201 猜。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlmodel import Session

from app import crud
from app.config import Settings
from app.integrations.byoc.client import ByocClient
from app.integrations.byoc.errors import (
    APIError,
    OrderValidationError,
    OutcomeUnknown,
    PollTimeout,
)
from app.models import Card, Task, utcnow
from app.services.card_stock import PLAN_BY_NAME, PlanSpec, replenish
from app.services.task_payload import decrypt_payload, encrypt_payload

logger = logging.getLogger(__name__)

KIND_OPEN_CARD = "open_card"
KIND_SUBSCRIPTION = "open_subscription"

STATUS_PENDING = "pending"
STATUS_WAITING_CARD = "waiting_card"
STATUS_WAITING_QUOTE = "waiting_quote"
STATUS_PROCESSING = "processing"
STATUS_WAITING_ACTION = "waiting_action"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})

CALLBACK_HEADER = "Kt-Secret"
CALLBACK_BACKOFF_SECONDS = (60, 300, 900, 3600, 10800)
CARD_RESERVE_SECONDS = 600
POLL_INTERVAL_SECONDS = 30
OPEN_CARD_RETRY_SECONDS = 120
UNKNOWN_RETRY_SECONDS = 300


def new_request_id(now: datetime | None = None) -> str:
    """BYOC 要求 8–96 位、字母开头、只含 [A-Za-z0-9._:-] 的固定请求号。"""
    stamp = (now or utcnow()).strftime("%Y%m%d%H%M%S")
    return f"order-{stamp}-{secrets.token_hex(4)}"


def parse_card_expiry(value: str | None) -> tuple[int, int] | None:
    """VCC 给的是 月/年，例如 11/28 → (11, 2028)。"""
    if not value or "/" not in value:
        return None
    month_text, year_text = value.split("/", 1)
    try:
        month = int(month_text.strip())
        year = int(year_text.strip())
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None
    if year < 100:
        year += 2000
    return month, year


def card_is_expired(expiry: tuple[int, int], now: datetime) -> bool:
    month, year = expiry
    return (year, month) < (now.year, now.month)


def sign_callback(body: dict[str, Any], secret: str) -> str:
    """按 key 升序拼 k=v&k1=v1，末尾拼 secret，取 MD5 hex。"""
    message = "&".join(f"{key}={body[key]}" for key in sorted(body)) + secret
    return hashlib.md5(message.encode("utf-8")).hexdigest()


def build_callback_body(task: Task) -> dict[str, str]:
    fields = {
        "task_id": task.id,
        "request_id": task.request_id,
        "status": task.status,
        "byoc_task_id": task.byoc_task_id,
        "payment_status": task.payment_status,
        "settlement": task.settlement,
        "display_amount": task.display_amount,
        "error": task.last_error,
    }
    return {key: str(value) for key, value in fields.items() if value is not None}


# --------------------------------------------------------------------------- 建任务


def create_subscription_task(
    session: Session,
    *,
    plan: str,
    session_payload: dict[str, Any],
    callback_url: str | None,
    settings: Settings,
    api_key_id: int | None = None,
) -> Task:
    spec = PLAN_BY_NAME.get(plan)
    if spec is None:
        raise ValueError(f"未知套餐：{plan}")
    if not settings.task_payload_key:
        raise RuntimeError("未配置 TASK_PAYLOAD_KEY，无法加密保存 session")
    if not isinstance(session_payload, dict) or not session_payload:
        raise ValueError("session 必须是非空对象")

    payload = {"plan": plan, "session": session_payload}
    ciphertext = encrypt_payload(payload, settings.task_payload_key.get_secret_value())
    return crud.create_task(
        session,
        kind=KIND_SUBSCRIPTION,
        plan=plan,
        request_id=new_request_id(),
        payload_ciphertext=ciphertext,
        callback_url=callback_url,
        api_key_id=api_key_id,
        run_at=utcnow(),
    )


def enqueue_open_card(session: Session, plan: str) -> Task | None:
    """同一个套餐只留一条未完成的开卡任务。"""
    if crud.find_open_card_task(session, plan) is not None:
        return None
    return crud.create_task(
        session,
        kind=KIND_OPEN_CARD,
        plan=plan,
        request_id=new_request_id(),
        run_at=utcnow(),
    )


# --------------------------------------------------------------------------- 执行


async def process_subscription_task(
    session: Session,
    task: Task,
    *,
    byoc: ByocClient,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    now = now or utcnow()
    logger.info(
        "订阅任务执行 task=%s request=%s plan=%s status=%s attempts=%d "
        "submit_attempts=%d card=%s byoc=%s payload=%s",
        task.id,
        task.request_id,
        task.plan,
        task.status,
        task.attempts,
        task.submit_attempts,
        task.card_id,
        task.byoc_task_id,
        bool(task.payload_ciphertext),
    )
    spec = PLAN_BY_NAME.get(task.plan or "")
    if spec is None:
        _finish_failed(session, task, f"未知套餐：{task.plan}", now)
        return

    # worker 认领后状态一定是 running，不能用 task.status 判断是否已经建过单；
    # 只要拿到过 byoc_task_id，就只轮询/找回，不再依赖 session 密文。
    if task.byoc_task_id or not task.payload_ciphertext:
        await _poll_order(session, task, byoc=byoc, now=now)
        return

    try:
        payload = _read_payload(task, settings)
    except ValueError as exc:
        _finish_failed(session, task, f"任务 payload 无法解密：{exc}", now)
        return

    quote = await _quote(session, task, byoc=byoc, spec=spec, settings=settings, now=now)
    if quote is None:
        return

    # 上次尝试可能已经锁过卡（validate 超时/网络异常时会保留锁），优先复用
    card = _reuse_own_card(session, task, spec=spec, now=now)
    if card is None:
        card = _claim_card(session, task, plan=spec.name, now=now)
    if card is None:
        enqueue_open_card(session, spec.name)
        _reschedule(session, task, STATUS_WAITING_CARD, 60, now)
        logger.warning("订阅任务等待可用卡：task=%s plan=%s", task.id, spec.name)
        return

    try:
        order = _build_order(task, spec, card, payload, quote.quote_id)
    except ValueError as exc:
        crud.release_card(session, card.id or 0, task.request_id)
        _finish_failed(session, task, str(exc), now)
        return

    await _submit_order(session, task, byoc=byoc, order=order, settings=settings, now=now)


async def process_open_card_task(
    session: Session,
    task: Task,
    *,
    ailink,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """补卡任务：只有开成、重试、失败三种结局，不再借用订阅的 waiting_card。

    开卡是异步的：同步响应经常没有 cardIds，三要素等 webhook 补，
    所以“已受理”按 processing 轮询；重试达到 OPEN_CARD_MAX_ATTEMPTS 就置 failed。
    """
    now = now or utcnow()
    logger.info(
        "补卡任务执行 task=%s plan=%s status=%s attempts=%d/%d",
        task.id,
        task.plan,
        task.status,
        task.submit_attempts,
        max(1, settings.open_card_max_attempts),
    )
    spec = PLAN_BY_NAME.get(task.plan or "")
    if spec is None or ailink is None:
        _finish_open_card_failed(session, task, "补卡缺少套餐配置或 AiLink 客户端", now)
        return
    report = await replenish(
        session,
        ailink,
        plans=[spec],
        usdt_rate=Decimal(str(settings.card_stock_usdt_usd_rate)),
        now=now,
    )
    if report.applied:
        _finish_open_card_succeeded(session, task, None, now)
        return
    if crud.find_usable_card(session, spec.name, min_balance=spec.unit_price) is not None:
        # 库存任务判定“有可用卡”，但订阅这边只是暂时抢不到卡锁；补卡任务到此为止
        _finish_open_card_succeeded(session, task, "已有可用卡，无需补卡", now)
        return
    if not report.missing:
        # 本轮没有缺口可开：同套餐还有在途的开卡申请，正在等 webhook 补三要素/首充。
        # 这不是补卡失败，不算 submit_attempts，也不覆盖上一轮的真实原因，
        # 否则等开卡结果的这几轮会把重试预算烧完，任务在 webhook 到达前就 failed 了。
        _reschedule(
            session,
            task,
            STATUS_PROCESSING,
            OPEN_CARD_RETRY_SECONDS,
            now,
            error=task.last_error,
        )
        logger.info(
            "补卡本轮没有缺口可开，等待开卡结果 task=%s plan=%s",
            task.id,
            spec.name,
        )
        return

    reason = report.failure_reasons.get(spec.name) or "本轮没有补到卡"
    task.submit_attempts += 1
    limit = max(1, settings.open_card_max_attempts)
    if task.submit_attempts >= limit:
        _finish_open_card_failed(
            session,
            task,
            f"{reason}（已尝试 {task.submit_attempts} 次，不再补卡）",
            now,
        )
        return
    _reschedule(
        session,
        task,
        STATUS_PROCESSING,
        OPEN_CARD_RETRY_SECONDS,
        now,
        error=reason[:500],
    )
    logger.warning(
        "补卡未完成，稍后重试 task=%s plan=%s 第 %d/%d 次：%s",
        task.id,
        spec.name,
        task.submit_attempts,
        limit,
        reason,
    )


def _finish_open_card_succeeded(
    session: Session,
    task: Task,
    note: str | None,
    now: datetime,
) -> None:
    task.status = STATUS_SUCCEEDED
    task.last_error = note
    crud.save_task(session, task)
    logger.info("补卡任务完成 task=%s plan=%s note=%s", task.id, task.plan, note or "-")


def _finish_open_card_failed(session: Session, task: Task, reason: str, now: datetime) -> None:
    task.status = STATUS_FAILED
    task.last_error = reason[:500]
    crud.save_task(session, task)
    logger.error(
        "补卡任务失败 task=%s plan=%s status=failed 尝试=%d：%s",
        task.id,
        task.plan,
        task.submit_attempts,
        reason,
    )


async def _quote(
    session: Session,
    task: Task,
    *,
    byoc: ByocClient,
    spec: PlanSpec,
    settings: Settings,
    now: datetime,
):
    try:
        quote = await byoc.quote(spec.byoc_plan_type, spec.billing_country)
    except (APIError, PollTimeout, httpx.HTTPError) as exc:
        _retry_or_fail(
            session,
            task,
            settings,
            f"获取报价失败：{exc}",
            now=now,
            status=STATUS_WAITING_QUOTE,
            delay_seconds=60,
        )
        return None
    if quote.valid_until <= now:
        _reschedule(
            session,
            task,
            STATUS_WAITING_QUOTE,
            30,
            now,
            error="报价已过期，重新报价",
        )
        return None
    if quote.plan_type.lower() != spec.byoc_plan_type:
        _retry_or_fail(
            session,
            task,
            settings,
            f"报价套餐不匹配：{quote.plan_type}",
            now=now,
            status=STATUS_WAITING_QUOTE,
            delay_seconds=60,
        )
        return None
    task.quote_id = quote.quote_id
    task.authorized_amount_minor = quote.authorized_amount_minor
    task.display_amount = quote.display_amount
    task.last_error = None
    crud.save_task(session, task)
    logger.info(
        "报价成功 task=%s quote=%s plan=%s 金额=%s display=%s",
        task.id,
        quote.quote_id,
        spec.name,
        quote.authorized_amount_minor,
        quote.display_amount,
    )
    return quote


def _read_payload(task: Task, settings: Settings) -> dict[str, Any]:
    if not task.payload_ciphertext:
        raise ValueError("任务没有 payload 密文")
    if not settings.task_payload_key:
        raise ValueError("未配置 TASK_PAYLOAD_KEY")
    payload = decrypt_payload(
        task.payload_ciphertext,
        settings.task_payload_key.get_secret_value(),
    )
    if not isinstance(payload.get("session"), dict) or not payload["session"]:
        raise ValueError("payload 缺少 session")
    return payload


def _reuse_own_card(
    session: Session,
    task: Task,
    *,
    spec: PlanSpec,
    now: datetime,
):
    """上一轮已经锁给自己的卡，校验超时/网络异常后下一轮继续用，不要重新选。"""
    if not task.card_id:
        return None
    card = session.get(Card, task.card_id)
    if card is None:
        task.card_id = None
        crud.save_task(session, task)
        return None

    expiry = parse_card_expiry(card.expiry)
    usable = (
        card.plan == spec.name
        and card.apply_status == "SUCCESS"
        and card.used_count < 3
        and card.balance >= spec.unit_price
        and bool(card.card_no and card.expiry and card.cvv)
        and expiry is not None
        and not card_is_expired(expiry, now)
    )
    if not usable:
        crud.release_card(session, card.id or 0, task.request_id)
        task.card_id = None
        crud.save_task(session, task)
        logger.info("原卡已不可用，释放并重新选卡 task=%s card=%s", task.id, card.card_id)
        return None

    if card.reserved_task_id == task.request_id and (
        card.reserved_until is None or card.reserved_until >= now
    ):
        crud.refresh_card_reservation(
            session,
            card.id or 0,
            task.request_id,
            until=now + timedelta(seconds=CARD_RESERVE_SECONDS),
        )
        logger.info(
            "任务复用已锁定的卡 task=%s card=%s balance=%s used=%d",
            task.id,
            card.card_id,
            card.balance,
            card.used_count,
        )
        return card

    # 锁已过期：尝试重新抢，被别的任务拿走就放弃
    if crud.claim_card(
        session,
        card.id or 0,
        task.request_id,
        until=now + timedelta(seconds=CARD_RESERVE_SECONDS),
        now=now,
    ):
        return card
    task.card_id = None
    crud.save_task(session, task)
    return None


def _claim_card(session: Session, task: Task, *, plan: str, now: datetime):
    spec = PLAN_BY_NAME[plan]
    candidates = crud.find_subscription_candidates(session, plan, now, min_balance=spec.unit_price)
    for card in candidates:
        expiry = parse_card_expiry(card.expiry)
        if expiry is None or card_is_expired(expiry, now):
            logger.error("卡有效期不可用，跳过：card=%s expiry=%s", card.card_id, card.expiry)
            continue
        if crud.claim_card(
            session,
            card.id or 0,
            task.request_id,
            until=now + timedelta(seconds=CARD_RESERVE_SECONDS),
            now=now,
        ):
            task.card_id = card.id
            crud.save_task(session, task)
            logger.info(
                "任务选卡成功 task=%s card=%s plan=%s balance=%s used=%d",
                task.id,
                card.card_id,
                plan,
                card.balance,
                card.used_count,
            )
            return card
    return None


def _build_order(
    task: Task,
    spec: PlanSpec,
    card,
    payload: dict[str, Any],
    quote_id: str,
) -> dict[str, Any]:
    expiry = parse_card_expiry(card.expiry)
    if expiry is None:
        raise ValueError(f"卡 {card.card_id} 有效期格式无法解析：{card.expiry}")
    month, year = expiry
    return {
        "request_id": task.request_id,
        "plan_type": spec.byoc_plan_type,
        "session": payload["session"],
        "card_number": card.card_no,
        "exp_month": month,
        "exp_year": year,
        "cvv": card.cvv,
        "bill_name": spec.byoc_plan_type,
        "auto_cancel_renew": True,
        "billing_country": spec.billing_country,
        "quote_id": quote_id,
    }


async def _submit_order(
    session: Session,
    task: Task,
    *,
    byoc: ByocClient,
    order: dict[str, Any],
    settings: Settings,
    now: datetime,
) -> None:
    # 校验阶段没有建单，失败/超时都能安全重试，session 也还要留着
    logger.info(
        "订单校验开始 task=%s request=%s quote=%s",
        task.id,
        task.request_id,
        order.get("quote_id"),
    )
    try:
        await byoc.validate_order(order)
    except PollTimeout as exc:
        _retry_or_fail(
            session,
            task,
            settings,
            f"校验响应超时：{exc}",
            now=now,
            delay_seconds=POLL_INTERVAL_SECONDS,
        )
        return
    except OrderValidationError as exc:
        _retry_or_fail(session, task, settings, str(exc), now=now)
        return
    except APIError as exc:
        _retry_or_fail(
            session,
            task,
            settings,
            f"BYOC 校验拒绝：HTTP {exc.status} {exc.message}",
            now=now,
        )
        return
    except httpx.HTTPError as exc:
        _retry_or_fail(
            session,
            task,
            settings,
            f"校验网络异常：{type(exc).__name__}",
            now=now,
        )
        return

    logger.info("订单校验通过 task=%s request=%s", task.id, task.request_id)
    # 一旦发出建单请求就不再重提：只按原 request_id 找回，session 也不再需要
    result: dict[str, Any] | None = None
    submitted = False
    try:
        result = await byoc.create("/orders", order)
        submitted = True
    except OutcomeUnknown as exc:
        submitted = True
        _mark_unknown(session, task, f"下单结果待确认：{exc}", now)
    except APIError as exc:
        if exc.recovered_task_id:
            submitted = True
            try:
                result = await byoc.status(exc.recovered_task_id)
            except (APIError, PollTimeout, httpx.HTTPError):
                _mark_unknown(session, task, "冲突后找回订单失败", now)
        else:
            # 明确拒绝说明没有建单，保留 payload 稍后重试
            _retry_or_fail(
                session,
                task,
                settings,
                f"BYOC 拒绝下单：HTTP {exc.status} {exc.message}",
                now=now,
            )
    except httpx.HTTPError as exc:
        submitted = True
        _mark_unknown(session, task, f"网络异常：{type(exc).__name__}", now)
    except Exception as exc:
        submitted = True
        _mark_unknown(session, task, f"下单异常：{type(exc).__name__}", now)

    if submitted:
        # 订单已经进了 BYOC，后面只按订单号/原请求号查询，session 密文不再需要
        logger.info(
            "订单已提交 task=%s request=%s byoc=%s",
            task.id,
            task.request_id,
            task.byoc_task_id or "-",
        )
        _clear_payload(session, task)
    if result is not None:
        _apply_order_state(session, task, result, now)


async def _poll_order(session: Session, task: Task, *, byoc: ByocClient, now: datetime) -> None:
    if task.card_id:
        # 轮询期间续锁，别让卡锁过期后被别的任务抢走
        crud.refresh_card_reservation(
            session,
            task.card_id,
            task.request_id,
            until=now + timedelta(seconds=CARD_RESERVE_SECONDS),
        )
    if task.byoc_task_id:
        try:
            order = await byoc.status(task.byoc_task_id)
        except (APIError, PollTimeout, httpx.HTTPError) as exc:
            _reschedule(
                session,
                task,
                STATUS_PROCESSING,
                POLL_INTERVAL_SECONDS,
                now,
                error=f"查询订单失败：{exc}"[:500],
            )
            return
        _apply_order_state(session, task, order, now)
        return

    # 提交结果未知时按原请求号找回，绝不换号重提
    try:
        order = await byoc.recover(task.request_id)
    except (APIError, OutcomeUnknown, httpx.HTTPError) as exc:
        _reschedule(
            session,
            task,
            STATUS_UNKNOWN,
            UNKNOWN_RETRY_SECONDS,
            now,
            error=f"找回订单失败：{exc}"[:500],
        )
        return
    if order is None:
        _reschedule(
            session,
            task,
            STATUS_UNKNOWN,
            UNKNOWN_RETRY_SECONDS,
            now,
            error="原请求号暂时查不到订单，稍后继续核对",
        )
        return
    _apply_order_state(session, task, order, now)


def _apply_order_state(session: Session, task: Task, order: dict[str, Any], now: datetime) -> None:
    previous_payment_status = task.payment_status
    task.byoc_task_id = order["task_id"]
    task.payment_status = order["payment_status"]
    task.settlement = order["settlement"]
    if task.payment_status != previous_payment_status:
        logger.info(
            "BYOC 订单状态变化 task=%s byoc=%s %s -> %s settlement=%s",
            task.id,
            task.byoc_task_id,
            previous_payment_status or "-",
            task.payment_status,
            task.settlement,
        )
    else:
        logger.debug(
            "BYOC 订单状态未变 task=%s byoc=%s status=%s",
            task.id,
            task.byoc_task_id,
            task.payment_status,
        )

    if order["payment_success"]:
        task.status = STATUS_SUCCEEDED
        task.last_error = None
        _release_card(session, task)
        _schedule_callback(session, task, now)
        logger.info("订阅开通成功 task=%s byoc=%s", task.id, task.byoc_task_id)
        return

    state = order["payment_status"]
    if state in {"failed", "cancelled"}:
        task.status = STATUS_FAILED
        task.last_error = "BYOC 订单失败"
        _release_card(session, task)
        _schedule_callback(session, task, now)
        logger.error("订阅开通失败 task=%s byoc=%s state=%s", task.id, task.byoc_task_id, state)
        return

    if state == "requires_action":
        _schedule_callback(session, task, now)
        _reschedule(session, task, STATUS_WAITING_ACTION, POLL_INTERVAL_SECONDS, now)
        logger.warning("订阅需要用户完成 3DS task=%s byoc=%s", task.id, task.byoc_task_id)
        return

    if state == "manual_review":
        _mark_unknown(session, task, "BYOC 转人工核对", now)
        return

    # queued / processing：继续轮询，卡保持锁定
    _reschedule(session, task, STATUS_PROCESSING, POLL_INTERVAL_SECONDS, now)


def _mark_unknown(session: Session, task: Task, reason: str, now: datetime) -> None:
    previous = task.status
    _schedule_callback(session, task, now)
    _reschedule(session, task, STATUS_UNKNOWN, UNKNOWN_RETRY_SECONDS, now, error=reason[:500])
    logger.error(
        "订阅任务结果未知，保留卡锁：task=%s request=%s %s -> %s reason=%s",
        task.id,
        task.request_id,
        previous,
        task.status,
        reason,
    )


def _retry_or_fail(
    session: Session,
    task: Task,
    settings: Settings,
    reason: str,
    *,
    now: datetime,
    status: str = STATUS_PENDING,
    delay_seconds: int | None = None,
) -> None:
    """BYOC 失败统一入口：保留 payload 按次数退避重试，达到上限才置为失败。

    计数只统计“和 BYOC 打交道失败”，和 Task.attempts（认领次数）分开，
    否则轮询订单也会把重试预算吃掉。
    """
    task.submit_attempts += 1
    message = reason[:500]
    limit = max(1, settings.subscription_max_submit_attempts)
    if task.submit_attempts >= limit:
        _finish_failed(
            session,
            task,
            f"{message}（已提交 {task.submit_attempts} 次，不再重试）",
            now,
        )
        return

    delay = (
        delay_seconds if delay_seconds is not None else _retry_delay(task.submit_attempts, settings)
    )
    _release_card(session, task)
    _reschedule(session, task, status, delay, now, error=message)
    logger.warning(
        "BYOC 失败，保留 payload 稍后重试 task=%s plan=%s 第 %d/%d 次 %ss 后：%s",
        task.id,
        task.plan,
        task.submit_attempts,
        limit,
        delay,
        message,
    )


def _retry_delay(attempt: int, settings: Settings) -> int:
    base = max(1, settings.subscription_retry_backoff_seconds)
    ceiling = max(base, settings.subscription_retry_max_backoff_seconds)
    return min(base * 2 ** max(0, attempt - 1), ceiling)


def _reschedule(
    session: Session,
    task: Task,
    status: str,
    delay_seconds: float,
    now: datetime,
    *,
    error: str | None = None,
) -> None:
    """把任务排到 `delay_seconds` 之后：状态和调度意图在同一个事务里落库。"""
    previous = task.status
    task.status = status
    task.last_error = error
    task.attempts += 1
    run_at = now + timedelta(seconds=delay_seconds)
    crud.save_task_with_outbox(
        session,
        task,
        kind=crud.OUTBOX_RUN,
        run_at=run_at,
        token=task.attempts,
    )
    if previous != status:
        logger.info(
            "任务状态变更 task=%s request=%s %s -> %s run_at=%s 第 %d 次 error=%s",
            task.id,
            task.request_id,
            previous,
            status,
            run_at,
            task.attempts,
            error or "-",
        )
    else:
        logger.debug(
            "任务仍为 %s task=%s request=%s run_at=%s 第 %d 次 error=%s",
            status,
            task.id,
            task.request_id,
            run_at,
            task.attempts,
            error or "-",
        )


def _finish_failed(session: Session, task: Task, error: str, now: datetime) -> None:
    previous = task.status
    task.status = STATUS_FAILED
    task.last_error = error[:500]
    # 终态不再需要 session 密文，失败和成功一样在这里清掉
    task.payload_ciphertext = None
    _release_card(session, task)
    _schedule_callback(session, task, now)
    logger.error(
        "订阅任务失败 task=%s request=%s %s -> failed：%s",
        task.id,
        task.request_id,
        previous,
        error,
    )
    crud.save_task(session, task)


def _release_card(session: Session, task: Task) -> None:
    """只释放卡锁，保留 task.card_id 作为“这单用了哪张卡”的追溯字段。"""
    if task.card_id:
        crud.release_card(session, task.card_id, task.request_id)


def _clear_payload(session: Session, task: Task) -> None:
    task.payload_ciphertext = None
    crud.save_task(session, task)


def _schedule_callback(session: Session, task: Task, now: datetime) -> None:
    """状态变了就排一次回调；已经有一条待投递的就不重复排，让那条带最新状态。"""
    if not task.callback_url or task.callback_delivered:
        crud.save_task(session, task)
        return
    if crud.has_pending_outbox(session, task.id or 0, crud.OUTBOX_CALLBACK):
        crud.save_task(session, task)
        return
    crud.save_task_with_outbox(
        session,
        task,
        kind=crud.OUTBOX_CALLBACK,
        run_at=now,
    )


# --------------------------------------------------------------------------- 回调


async def deliver_callback(
    session: Session,
    task: Task,
    *,
    http_client: httpx.AsyncClient,
    settings: Settings,
    now: datetime | None = None,
) -> bool:
    now = now or utcnow()
    if not task.callback_url or task.callback_delivered:
        return True
    secret = (
        settings.byoc_callback_secret.get_secret_value() if settings.byoc_callback_secret else ""
    )
    body = build_callback_body(task)
    headers = {CALLBACK_HEADER: sign_callback(body, secret)} if secret else {}
    try:
        response = await http_client.post(task.callback_url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        _reschedule_callback(session, task, now, f"回调网络异常：{type(exc).__name__}", settings)
        return False
    if 200 <= response.status_code < 300:
        task.callback_delivered = True
        task.callback_attempts += 1
        crud.save_task(session, task)
        logger.info("订阅回调成功 task=%s status=%s", task.id, task.status)
        return True
    _reschedule_callback(session, task, now, f"回调返回 HTTP {response.status_code}", settings)
    return False


def _reschedule_callback(
    session: Session,
    task: Task,
    now: datetime,
    error: str,
    settings: Settings,
) -> None:
    task.callback_attempts += 1
    task.last_error = error[:500]
    if task.callback_attempts >= settings.subscription_callback_max_attempts:
        logger.error("订阅回调放弃 task=%s attempts=%d", task.id, task.callback_attempts)
        crud.save_task(session, task)
        return
    delay = CALLBACK_BACKOFF_SECONDS[
        min(task.callback_attempts - 1, len(CALLBACK_BACKOFF_SECONDS) - 1)
    ]
    logger.error("订阅回调失败，稍后重试 task=%s：%s", task.id, error)
    crud.save_task_with_outbox(
        session,
        task,
        kind=crud.OUTBOX_CALLBACK,
        run_at=now + timedelta(seconds=delay),
    )
