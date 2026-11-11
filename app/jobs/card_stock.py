"""卡库存扫描任务：进程内 asyncio 循环 + 数据库租约。

lease 只保证同一时间只有一个实例在跑；任务内部所有网络失败都落 error 日志后继续下一轮。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from decimal import Decimal

from sqlmodel import Session

from app import crud
from app.integrations.ailink.client import AiLinkClient
from app.models import utcnow
from app.services.card_stock import StockReport, replenish

logger = logging.getLogger(__name__)

LEASE_NAME = "card_stock"
LEASE_TTL = timedelta(minutes=5)

SessionFactory = Callable[[], AbstractContextManager[Session]]


async def run_once(
    *,
    client: AiLinkClient,
    session_factory: SessionFactory,
    usdt_rate: Decimal,
    pending_window: timedelta | None = None,
    now: datetime | None = None,
) -> StockReport | None:
    """跑一轮；别的副本持锁时返回 None。"""
    with session_factory() as session:
        crud.ensure_job_lease(session, LEASE_NAME)
        if not crud.acquire_job_lease(
            session,
            LEASE_NAME,
            locked_until=utcnow() + LEASE_TTL,
        ):
            logger.info("卡库存扫描已被其他实例持有，跳过本轮")
            return None
        try:
            report = await replenish(
                session,
                client,
                usdt_rate=usdt_rate,
                pending_window=pending_window,
                now=now,
            )
        finally:
            crud.release_job_lease(session, LEASE_NAME)

    logger.info(
        "卡库存扫描完成：缺失=%s 已申请=%s 失败=%s 兑换=%s USDT",
        ",".join(report.missing) or "-",
        ",".join(report.applied) or "-",
        ",".join(report.failed) or "-",
        report.converted_usdt,
    )
    return report


async def run_forever(
    *,
    client: AiLinkClient,
    session_factory: SessionFactory,
    interval_seconds: int,
    usdt_rate: Decimal,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    sleep = sleep or asyncio.sleep
    logger.info("卡库存扫描任务启动，间隔 %ss", interval_seconds)
    while True:
        try:
            await run_once(
                client=client,
                session_factory=session_factory,
                usdt_rate=usdt_rate,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("卡库存扫描任务异常，等待下一轮")
        await sleep(interval_seconds)
