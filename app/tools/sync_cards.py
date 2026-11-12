"""把 AiLink 平台上已有的卡同步进本地 card 表。

平台列表只返回脱敏卡号，所以导入的卡不写 card_no/expiry/cvv，也不会被订阅选卡使用；
已有卡只更新余额、批次和停用状态，不动 used_count 和卡三要素。

    uv run python -m app.tools.sync_cards
    uv run python -m app.tools.sync_cards --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app import crud
from app.db import session_scope
from app.integrations.ailink.client import AiLinkClient
from app.services.card_events import parse_platform_time
from app.services.card_stock import PLAN_BY_NAME

logger = logging.getLogger(__name__)

# 平台卡状态 -> 本地 apply_status
APPLY_STATUS_MAP = {
    "ACTIVE": "SUCCESS",
    "FROZEN": "SUCCESS",
    "OPENING": "PENDING",
    "CANCEL_REQUESTED": "SUCCESS",
    "CANCELLED": "FAILED",
    "EXPIRED": "FAILED",
}

# 这些状态在本地一律不可用
UNAVAILABLE_STATUSES = {"FROZEN", "CANCELLED", "EXPIRED", "CANCEL_REQUESTED"}


@dataclass
class SyncReport:
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    rows: list[tuple[str, str, str, str, str]] = field(default_factory=list)

    def add_row(self, card_id: str, masked: str, status: str, balance: str, action: str) -> None:
        self.rows.append((card_id, masked, status, balance, action))


def plan_from_remark(remark: str | None) -> str:
    """平台没有我们的套餐字段，只能从备注里认；认不出就标 unknown。"""
    text = (remark or "").lower()
    for name in PLAN_BY_NAME:
        if name in text:
            return name
    return "unknown"


def parse_balance(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _upsert_card(
    session: Session,
    item: dict[str, Any],
    report: SyncReport,
    *,
    dry_run: bool,
) -> None:
    card_id = item.get("cardId")
    masked = item.get("cardNoMask") or ""
    platform_status = str(item.get("cardStatus") or "").upper()
    balance = parse_balance(item.get("balance"))

    if not card_id:
        report.skipped += 1
        report.add_row("-", masked, platform_status, str(balance), "缺少 cardId，跳过")
        return

    existing = crud.get_card_by_platform_id(session, card_id)
    if existing is not None:
        changed = False
        if existing.balance != balance:
            existing.balance = balance
            changed = True
        if existing.plan == "unknown":
            inferred = plan_from_remark(item.get("remark"))
            if inferred != "unknown":
                existing.plan = inferred
                changed = True
        batch_no = item.get("batchNo")
        if batch_no and existing.batch_no != batch_no:
            existing.batch_no = batch_no
            changed = True
        if platform_status in UNAVAILABLE_STATUSES and existing.enabled:
            existing.enabled = False
            changed = True
        if changed and not dry_run:
            crud.save_card(session, existing)
        report.updated += 1
        report.add_row(card_id, masked, platform_status, str(balance), "已存在，更新")
        return

    plan = plan_from_remark(item.get("remark"))
    request_no = f"import_{card_id}"[:64]
    if dry_run:
        report.imported += 1
        report.add_row(card_id, masked, platform_status, str(balance), f"将导入 plan={plan}")
        return

    try:
        card = crud.create_card_intent(
            session,
            plan=plan,
            request_no=request_no,
            recharge_amount=balance,
        )
    except (IntegrityError, crud.ConflictError):
        session.rollback()
        report.failed += 1
        report.add_row(card_id, masked, platform_status, str(balance), "request_no 冲突，跳过")
        return

    card.card_id = card_id
    card.batch_no = item.get("batchNo") or None
    card.apply_status = APPLY_STATUS_MAP.get(platform_status, "SUCCESS")
    card.applied_at = parse_platform_time(item.get("createTime"))
    # 列表没有完整卡号/CVV，导入的卡不参与自动订阅选卡
    card.enabled = False
    crud.save_card(session, card)
    report.imported += 1
    report.add_row(card_id, masked, platform_status, str(balance), f"已导入 plan={plan}")


async def sync_cards(
    session: Session,
    client: Any,
    *,
    page_size: int = 100,
    dry_run: bool = False,
) -> SyncReport:
    report = SyncReport()
    page = 1
    while page <= 100:
        data = await client.query_cards(page_num=page, page_size=page_size)
        items = data.get("list") or []
        if not items:
            break
        for item in items:
            _upsert_card(session, item, report, dry_run=dry_run)
        total = int(data.get("total") or 0)
        if page * page_size >= total:
            break
        page += 1
    return report


def _print_report(report: SyncReport, *, dry_run: bool) -> None:
    head = "预演（不写库）" if dry_run else "同步完成"
    print(
        f"{head}：新增 {report.imported}，更新 {report.updated}，"
        f"跳过 {report.skipped}，失败 {report.failed}"
    )
    if not report.rows:
        return
    print(f"\n{'cardId':<44}{'卡号':<24}{'平台状态':<16}{'余额':>10}  动作")
    for card_id, masked, status, balance, action in report.rows:
        print(f"{card_id:<44}{masked:<24}{status:<16}{balance:>10}  {action}")


def run(*, page_size: int = 100, dry_run: bool = False) -> int:
    from app.config import get_settings

    if not get_settings().ailink_configured:
        print("未配置 AILINK_API_KEY / AILINK_API_SECRET")
        return 1
    with session_scope() as session:
        client = AiLinkClient.from_settings()

        async def _run() -> SyncReport:
            try:
                return await sync_cards(
                    session,
                    client,
                    page_size=page_size,
                    dry_run=dry_run,
                )
            finally:
                await client.aclose()

        report = asyncio.run(_run())
    _print_report(report, dry_run=dry_run)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 AiLink 平台已有卡同步到本地 card 表")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写数据库")
    parser.add_argument("--page-size", type=int, default=100, help="每页条数，默认 100")
    args = parser.parse_args(argv)
    if not 1 <= args.page_size <= 100:
        parser.error("--page-size 必须在 1–100 之间")
    return run(page_size=args.page_size, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
