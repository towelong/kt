"""卡库存：保证每个套餐至少一张可用卡。

一轮流程：查缺口 → 查余额（USD 不够就 USDT→USD 兑换）→ 选美国 BIN → 申请卡。
卡三要素只从开卡 webhook 补，所以刚申请的卡先占位、暂不启用。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from sqlmodel import Session

from app import crud
from app.config import get_settings
from app.integrations.ailink.client import AiLinkClient, new_request_no
from app.integrations.ailink.errors import (
    AiLinkBusinessError,
    AiLinkError,
    AiLinkServerError,
    AiLinkTransportError,
)
from app.integrations.ailink.models import CardApplyRequest, CardBin
from app.models import CardPlan, utcnow

logger = logging.getLogger(__name__)

MAX_USES = 3
DEFAULT_USDT_RATE = Decimal("0.997")


@dataclass(frozen=True)
class PlanSpec:
    plan: CardPlan
    unit_price: Decimal
    # BYOC 的套餐标识和我们内部名字不一样，报价和下单都用这个
    byoc_plan_type: str
    billing_country: str

    @property
    def name(self) -> str:
        return self.plan.value

    @property
    def initial_amount(self) -> Decimal:
        """一张卡要撑满 3 次交易，首充 = 单价 × 3。"""
        return (self.unit_price * MAX_USES).quantize(Decimal("0.01"))


PLAN_SPECS: tuple[PlanSpec, ...] = (
    PlanSpec(CardPlan.PLUS, Decimal("16"), "plus", "ph"),
    PlanSpec(CardPlan.PRO100, Decimal("90"), "pro5", "eg"),
    PlanSpec(CardPlan.PRO200, Decimal("150"), "pro20", "ph"),
    PlanSpec(CardPlan.PRO500, Decimal("470"), "promax", "eg"),
)

PLAN_BY_NAME: dict[str, PlanSpec] = {spec.name: spec for spec in PLAN_SPECS}


@dataclass
class StockReport:
    missing: list[str] = field(default_factory=list)
    converted_usdt: Decimal = Decimal("0")
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


async def replenish(
    session: Session,
    client: AiLinkClient,
    *,
    usdt_rate: Decimal = DEFAULT_USDT_RATE,
    pending_window: timedelta | None = None,
    now: datetime | None = None,
    plans: Sequence[PlanSpec] | None = None,
) -> StockReport:
    """跑一轮补卡。网络失败不抛给调用方，全部落 error 日志并返回报告。"""
    now = now or utcnow()
    pending_window = pending_window or timedelta(minutes=get_settings().card_stock_pending_minutes)
    report = StockReport()

    missing = _plans_missing_card(session, now, pending_window, plans or PLAN_SPECS)
    if not missing:
        return report
    report.missing = [spec.name for spec in missing]

    total = sum((spec.initial_amount for spec in missing), Decimal("0"))
    if not await _ensure_usd_balance(client, total, usdt_rate, report):
        return report

    bins = await client.list_card_bins()
    if bins.quota.remaining_card_count <= 0:
        logger.error(
            "开卡配额已用尽：已开 %d / 上限 %d，本轮不补卡",
            bins.quota.issued_card_count,
            bins.quota.max_card_count,
        )
        return report

    for spec in missing:
        bin_config = _pick_us_bin(bins.bins)
        if bin_config is None:
            logger.error(
                "没有可用的美国卡 BIN：plan=%s 首充=%s USD",
                spec.name,
                spec.initial_amount,
            )
            report.failed.append(spec.name)
            continue
        await _issue_one(session, client, spec, bin_config, report)

    return report


def _plans_missing_card(
    session: Session,
    now: datetime,
    pending_window: timedelta,
    plans: Sequence[PlanSpec],
) -> list[PlanSpec]:
    missing: list[PlanSpec] = []
    since = now - pending_window
    for spec in plans:
        if crud.find_usable_card(session, spec.name) is not None:
            continue
        in_flight = crud.get_recent_incomplete_card(session, spec.name, since)
        if in_flight is not None:
            logger.warning(
                "套餐 %s 有未完成的开卡申请，跳过本轮 requestNo=%s status=%s",
                spec.name,
                in_flight.request_no,
                in_flight.apply_status,
            )
            continue
        missing.append(spec)
    return missing


async def _ensure_usd_balance(
    client: AiLinkClient,
    total: Decimal,
    usdt_rate: Decimal,
    report: StockReport,
) -> bool:
    balance = await client.get_account_balance()
    usd = balance.get("USD")
    usd_available = usd.available_balance if usd else Decimal("0")
    if usd_available >= total:
        return True

    gap = total - usd_available
    usdt = balance.get("USDT")
    usdt_available = usdt.available_balance if usdt else Decimal("0")
    if usdt_rate <= 0:
        logger.error("CARD_STOCK_USDT_USD_RATE 配置不合法：%s", usdt_rate)
        return False
    usdt_needed = (gap / usdt_rate).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    if usdt_available < usdt_needed:
        logger.error(
            "开卡余额不足：需要 %s USD，USD 可用 %s，USDT 可用 %s（按 %s 折算还需 %s USDT）",
            total,
            usd_available,
            usdt_available,
            usdt_rate,
            usdt_needed,
        )
        return False

    try:
        converted = await client.convert_balance("USDT", "USD", usdt_needed)
    except AiLinkError as exc:
        logger.error("USDT 兑换 USD 失败：%s", exc)
        return False
    report.converted_usdt = converted.amount
    logger.info(
        "USDT 兑换 USD 成功：%s USDT -> %s USD（手续费 %s）",
        converted.amount,
        converted.received_amount,
        converted.fee_amount,
    )

    balance = await client.get_account_balance()
    usd = balance.get("USD")
    usd_available = usd.available_balance if usd else Decimal("0")
    if usd_available < total:
        logger.error(
            "兑换后 USD 仍不足：需要 %s，可用 %s，请人工核对",
            total,
            usd_available,
        )
        return False
    return True


def _pick_us_bin(bins: list[CardBin]) -> CardBin | None:
    """只按发卡地区筛美国，其余（卡类型、币种、最低金额）不限制。"""
    for item in bins:
        if (item.issuing_country or "").upper() == "US":
            return item
    return None


async def _issue_one(
    session: Session,
    client: AiLinkClient,
    spec: PlanSpec,
    bin_config: CardBin,
    report: StockReport,
) -> None:
    request_no = new_request_no(f"kaika_{spec.name}_")
    card = crud.create_card_intent(
        session,
        plan=spec.name,
        request_no=request_no,
        recharge_amount=spec.initial_amount,
    )
    payload = CardApplyRequest(
        request_no=request_no,
        card_type="PREPAID",
        card_bin_id=bin_config.bin_id,
        card_count=1,
        recharge_amount=spec.initial_amount,
        remark=f"stock-{spec.name}"[:40],
    )
    try:
        result = await client.apply_cards(payload)
    except AiLinkBusinessError as exc:
        logger.error("开卡被平台拒绝 plan=%s requestNo=%s：%s", spec.name, request_no, exc)
        crud.mark_card_failed(session, card)
        report.failed.append(spec.name)
        return
    except (AiLinkTransportError, AiLinkServerError) as exc:
        logger.error(
            "开卡结果未知 plan=%s requestNo=%s：%s（保留占位等待核对，不换号重提）",
            spec.name,
            request_no,
            exc,
        )
        report.failed.append(spec.name)
        return
    except AiLinkError as exc:
        logger.error("开卡失败 plan=%s requestNo=%s：%s", spec.name, request_no, exc)
        crud.mark_card_failed(session, card)
        report.failed.append(spec.name)
        return

    if result.apply_status == "FAILED" or result.task_status == "FAILED":
        logger.error(
            "开卡任务失败 plan=%s requestNo=%s applyStatus=%s taskStatus=%s",
            spec.name,
            request_no,
            result.apply_status,
            result.task_status,
        )
        crud.mark_card_failed(session, card)
        report.failed.append(spec.name)
        return

    if not result.card_ids:
        # 卡池没立即交付：保留 pending 占位，等 webhook 认领
        logger.error(
            "开卡同步响应没有 cardIds，等待开卡 webhook plan=%s requestNo=%s applyStatus=%s",
            spec.name,
            request_no,
            result.apply_status,
        )
        card.apply_status = result.apply_status or "PENDING"
        card.initial_funding_status = result.initial_funding_status
        card.batch_no = result.batch_no
        crud.save_card(session, card)
        report.failed.append(spec.name)
        return

    card.card_id = result.card_ids[0]
    card.batch_no = result.batch_no
    card.apply_status = result.apply_status or "SUCCESS"
    card.initial_funding_status = result.initial_funding_status
    card.applied_at = utcnow()
    crud.save_card(session, card)
    for index, extra_card_id in enumerate(result.card_ids[1:], start=2):
        _save_extra_card(session, spec, request_no, extra_card_id, index)
    if result.initial_funding_status == "FAILED":
        logger.error(
            "开卡首充失败 plan=%s requestNo=%s card=%s",
            spec.name,
            request_no,
            card.card_id,
        )
    logger.info(
        "开卡申请成功 plan=%s requestNo=%s card=%s applyStatus=%s 首充=%s",
        spec.name,
        request_no,
        card.card_id,
        card.apply_status,
        result.initial_funding_status,
    )
    report.applied.append(spec.name)


def _save_extra_card(
    session: Session,
    spec: PlanSpec,
    request_no: str,
    card_id: str,
    index: int,
) -> None:
    """一次申请返回多张卡时，除占位行外逐张补记录（默认只开 1 张，这里兜底）。"""
    extra_request_no = f"{request_no[:56]}_{index:02d}"
    extra = crud.create_card_intent(
        session,
        plan=spec.name,
        request_no=extra_request_no,
        recharge_amount=spec.initial_amount,
    )
    extra.card_id = card_id
    extra.apply_status = "SUCCESS"
    extra.applied_at = utcnow()
    crud.save_card(session, extra)
