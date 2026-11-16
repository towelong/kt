"""数据库任务队列 worker：补卡、订阅下单、回调重试都在这里跑。

单进程内循环；多副本靠条件 UPDATE 抢任务，同一任务同一时间只被一个实例处理。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
from sqlmodel import Session

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
                logger.debug("本轮没有可执行任务")
                break
            processed += 1
            started = time.perf_counter()
            logger.info(
                "任务开始 task=%s kind=%s plan=%s status=%s attempts=%d "
                "submit_attempts=%d card=%s byoc=%s",
                task.id,
                task.kind,
                task.plan,
                task.status,
                task.attempts,
                task.submit_attempts,
                task.card_id,
                task.byoc_task_id,
            )
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
                    else:
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
            finally:
                logger.info(
                    "任务结束 task=%s kind=%s status=%s 耗时=%.2fs next_run_at=%s error=%s",
                    task.id,
                    task.kind,
                    task.status,
                    time.perf_counter() - started,
                    task.next_run_at,
                    task.last_error or "-",
                )

        for task in crud.list_due_callbacks(session, now):
            await deliver_callback(
                session,
                task,
                http_client=callback_http,
                settings=settings,
                now=now,
            )

    return processed


def log_queue_status(session: Session) -> None:
    """打一份任务队列快照，空闲时也能看到 worker 还活着。"""
    counts = crud.task_status_counts(session)
    running = crud.list_running_tasks(session, now=utcnow())
    summary = " ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "空"
    detail = (
        " | ".join(
            f"task={task.id} kind={task.kind} plan={task.plan} claimed_until={task.claimed_until}"
            for task in running
        )
        or "-"
    )
    logger.info("任务队列快照 %s 运行中=%s", summary, detail)


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
    logger.info(
        "订阅任务 worker 启动，间隔 %ss，状态快照间隔 %ss",
        interval_seconds,
        settings.task_status_log_seconds,
    )
    try:
        with session_factory() as session:
            log_queue_status(session)
    except Exception:
        logger.exception("任务队列快照失败")

    last_snapshot = time.monotonic()
    try:
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

            if settings.task_status_log_seconds > 0:
                current = time.monotonic()
                if current - last_snapshot >= settings.task_status_log_seconds:
                    last_snapshot = current
                    try:
                        with session_factory() as session:
                            log_queue_status(session)
                    except Exception:
                        logger.exception("任务队列快照失败")
            await sleep(interval_seconds)
    finally:
        logger.info("订阅任务 worker 停止")
