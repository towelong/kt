"""数据库任务队列 worker：补卡、订阅下单、回调重试都在这里跑。

单进程内循环；多副本靠条件 UPDATE 抢任务，同一任务同一时间只被一个实例处理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx

from app import crud
from app.config import Settings
from app.integrations.ailink.client import AiLinkClient
from app.integrations.byoc.client import ByocClient
from app.jobs.card_stock import SessionFactory
from app.models import utcnow
from app.services.subscriptions import (
    KIND_OPEN_CARD,
    KIND_SUBSCRIPTION,
    STATUS_FAILED,
    deliver_callback,
    process_open_card_task,
    process_subscription_task,
)

logger = logging.getLogger(__name__)

CLAIM_TTL = timedelta(minutes=5)
ERROR_RETRY_SECONDS = 60
DEFAULT_MAX_TASKS = 10


async def run_once(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    byoc: ByocClient | None,
    ailink: AiLinkClient | None,
    callback_http: httpx.AsyncClient,
    now=None,
    max_tasks: int = DEFAULT_MAX_TASKS,
) -> int:
    now = now or utcnow()
    processed = 0
    with session_factory() as session:
        while processed < max_tasks:
            task = crud.claim_next_task(
                session,
                now=now,
                claim_until=now + CLAIM_TTL,
            )
            if task is None:
                break
            processed += 1
            try:
                if task.kind == KIND_OPEN_CARD:
                    await process_open_card_task(
                        session, task, ailink=ailink, settings=settings, now=now
                    )
                elif task.kind == KIND_SUBSCRIPTION:
                    if byoc is None:
                        # 配置缺失不是“等卡”，重试也不会自己好，直接失败并留原因
                        task.status = STATUS_FAILED
                        task.last_error = "未配置 BYOC，无法执行订阅任务"
                        task.next_run_at = now
                        task.claimed_until = None
                        crud.save_task(session, task)
                        continue
                    await process_subscription_task(
                        session, task, byoc=byoc, settings=settings, now=now
                    )
                else:
                    task.status = "failed"
                    task.last_error = f"未知任务类型：{task.kind}"
                    task.claimed_until = None
                    crud.save_task(session, task)
            except Exception as exc:  # 任务不能把 worker 循环带崩
                logger.exception("任务执行异常 task=%s kind=%s", task.id, task.kind)
                task.status = "pending"
                task.last_error = str(exc)[:500]
                task.next_run_at = now + timedelta(seconds=ERROR_RETRY_SECONDS)
                task.claimed_until = None
                crud.save_task(session, task)

        for task in crud.list_due_callbacks(session, now):
            await deliver_callback(
                session,
                task,
                http_client=callback_http,
                settings=settings,
                now=now,
            )

    return processed


async def run_forever(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    byoc: ByocClient | None,
    ailink: AiLinkClient | None,
    callback_http: httpx.AsyncClient,
    interval_seconds: int,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    sleep = sleep or asyncio.sleep
    logger.info("订阅任务 worker 启动，间隔 %ss", interval_seconds)
    while True:
        try:
            await run_once(
                session_factory=session_factory,
                settings=settings,
                byoc=byoc,
                ailink=ailink,
                callback_http=callback_http,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("订阅任务 worker 异常，等待下一轮")
        await sleep(interval_seconds)
