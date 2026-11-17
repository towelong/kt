"""arq worker：数据库是状态机，Redis 负责“什么时候、在哪个进程里跑”。

启动（独立进程，和 API 分开部署）：

    uv run arq app.jobs.arq_worker.WorkerSettings

调度链路：

1. 业务代码改状态时，在同一个事务里往 `task_outbox` 写一条“什么时候再跑”
   （`crud.save_task_with_outbox`）；
2. `publish_outbox` 定时扫这些行，换算成 `_defer_by` 投成 arq 延迟消息，
   `_job_id` 用行号保证同一条只投一次；
3. worker 真正开始执行时删掉 outbox 行（ack）；没 ack 的行会被反复重投，Redis 丢消息也能自愈；
4. 重试语义（`submit_attempts`、回调退避、终态）仍然写在数据库里，arq 只负责触发。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from arq import cron
from arq.connections import RedisSettings
from arq.worker import func

from app import crud
from app.config import Settings, get_settings
from app.db import session_scope
from app.integrations.ailink.client import AiLinkClient
from app.integrations.byoc.client import ByocClient
from app.integrations.ratelimit import RateLimiter
from app.jobs.executor import execute_task, log_queue_status
from app.log_config import configure_logging
from app.models import utcnow
from app.services.card_stock import replenish
from app.services.subscriptions import (
    KIND_OPEN_CARD,
    KIND_SUBSCRIPTION,
    TERMINAL_STATUSES,
    deliver_callback,
)

logger = logging.getLogger(__name__)

# 一轮 publisher 最多处理多少条调度意图
PUBLISH_BATCH = 20
# 任务抛异常（代码 bug、DB 抖动）后多久重试一次
ERROR_RETRY_SECONDS = 60

_JOB_BY_KIND = {
    crud.OUTBOX_RUN: "run_task",
    crud.OUTBOX_CALLBACK: "deliver_task_callback",
}


# --------------------------------------------------------------------------- 投递


def _interval_cron(fn, seconds: int, *, unique: bool = True):
    """arq 的 cron 只认集合：把“每 N 秒”折成秒/分钟集合，多副本只跑一次。"""
    seconds = max(1, seconds)
    if seconds < 60:
        return cron(fn, second=set(range(0, 60, seconds)), unique=unique)
    step = max(1, seconds // 60)
    return cron(fn, minute=set(range(0, 60, step)), second={0}, unique=unique)


async def publish_outbox(ctx, *, now: datetime | None = None) -> int:
    """把到期的调度意图投成 arq 延迟消息；行本身等 worker ack 才删。"""
    now = now or utcnow()
    redis = ctx["redis"]
    # BYOC 没配置时只投补卡任务：订阅任务留在库里等配置修好，不要领走直接判失败
    run_kinds = (
        [KIND_OPEN_CARD, KIND_SUBSCRIPTION] if ctx.get("byoc") is not None else [KIND_OPEN_CARD]
    )

    published = 0
    with ctx["session_factory"]() as session:
        rows = crud.list_outbox(
            session,
            limit=PUBLISH_BATCH,
            run_kinds=run_kinds,
        )
        for row in rows:
            job_name = _JOB_BY_KIND.get(row.kind)
            if job_name is None:
                logger.error("未知调度类型，丢弃 outbox=%s kind=%s", row.id, row.kind)
                crud.delete_outbox(session, row.id or 0)
                continue
            args = (
                (row.task_id, row.token, row.id)
                if row.kind == crud.OUTBOX_RUN
                else (row.task_id, row.id)
            )
            delay = max(0.0, (row.run_at - now).total_seconds())
            try:
                await redis.enqueue_job(
                    job_name,
                    *args,
                    _job_id=f"kt:{row.kind}:{row.id}",
                    _defer_by=delay or None,
                )
            except Exception:
                logger.exception("投递调度失败，下一轮重试 outbox=%s", row.id)
                break
            published += 1

        _maybe_log_snapshot(ctx, session, now)

    if published:
        logger.debug("publisher 投递调度 %d 条", published)
    return published


def _maybe_log_snapshot(ctx, session, now: datetime) -> None:
    """按 task_status_log_seconds 打队列快照，0 表示不打。"""
    interval = ctx["settings"].task_status_log_seconds
    if interval <= 0:
        return
    last = ctx.get("snapshot_at")
    if last is not None and (now - last).total_seconds() < interval:
        return
    ctx["snapshot_at"] = now
    log_queue_status(session, now=now)


# --------------------------------------------------------------------------- actor


async def run_task(
    ctx,
    task_id: int,
    token: int,
    outbox_id: int,
    *,
    now: datetime | None = None,
) -> None:
    """执行一条任务。`token` 是调度时的 attempts，旧消息直接丢弃。"""
    now = now or utcnow()
    settings: Settings = ctx["settings"]

    with ctx["session_factory"]() as session:
        try:
            task = crud.get_task(session, task_id)
        except crud.NotFoundError:
            logger.error("任务不存在，丢弃消息 task=%s", task_id)
            crud.delete_outbox(session, outbox_id)
            return
        if task.status in TERMINAL_STATUSES:
            logger.info("任务已是终态，跳过重复投递 task=%s status=%s", task.id, task.status)
            crud.delete_outbox(session, outbox_id)
            return
        if task.attempts != token:
            logger.warning(
                "任务已重新排期，丢弃旧消息 task=%s 消息=%d 当前=%d",
                task.id,
                token,
                task.attempts,
            )
            crud.delete_outbox(session, outbox_id)
            return

        kind = task.kind
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
            await execute_task(
                session,
                task,
                byoc=ctx.get("byoc"),
                ailink=ctx.get("ailink"),
                settings=settings,
                now=now,
            )
        except Exception as exc:  # 任务不能把 worker 带崩，写回数据库再排一次
            logger.exception("任务执行异常 task=%s kind=%s", task.id, task.kind)
            task.status = "pending"
            task.last_error = str(exc)[:500]
            task.attempts += 1
            crud.save_task_with_outbox(
                session,
                task,
                kind=crud.OUTBOX_RUN,
                run_at=now + timedelta(seconds=ERROR_RETRY_SECONDS),
                token=task.attempts,
            )
        finally:
            logger.info(
                "任务结束 task=%s kind=%s status=%s 耗时=%.2fs error=%s",
                task.id,
                kind,
                task.status,
                time.perf_counter() - started,
                task.last_error or "-",
            )

        # 跑完才 ack：进程被杀或重排失败时行还在，publisher 会重新投
        # （arq 侧同一个 _job_id 会去重）
        crud.delete_outbox(session, outbox_id)


async def deliver_task_callback(
    ctx,
    task_id: int,
    outbox_id: int,
    *,
    now: datetime | None = None,
) -> None:
    """投递一条任务回调；是否已送达、下次什么时候重试都在数据库里。"""
    now = now or utcnow()
    with ctx["session_factory"]() as session:
        try:
            task = crud.get_task(session, task_id)
        except crud.NotFoundError:
            logger.error("回调任务不存在，丢弃消息 task=%s", task_id)
            return
        if task.callback_delivered or not task.callback_url:
            crud.delete_outbox(session, outbox_id)
            return
        try:
            await deliver_callback(
                session,
                task,
                http_client=ctx["callback_http"],
                settings=ctx["settings"],
                now=now,
            )
        except Exception:
            # 行留着，publisher 下一轮会重新投；deliver_callback 自己会处理业务失败
            logger.exception("回调投递异常 task=%s", task.id)
            return
        crud.delete_outbox(session, outbox_id)


# --------------------------------------------------------------------------- 卡库存


async def card_stock_scan(ctx, *, now: datetime | None = None) -> None:
    """定时补卡，替代原来的进程内循环 + job_lease。"""
    settings: Settings = ctx["settings"]
    client = ctx.get("ailink")
    if client is None:
        logger.error("卡库存扫描缺少 AiLink 客户端，跳过本轮")
        return

    now = now or utcnow()
    started = time.perf_counter()
    with ctx["session_factory"]() as session:
        report = await replenish(
            session,
            client,
            usdt_rate=Decimal(str(settings.card_stock_usdt_usd_rate)),
            now=now,
        )
    logger.info(
        "卡库存扫描完成：缺失=%s 已申请=%s 失败=%s 兑换=%s USDT 耗时=%.2fs",
        ",".join(report.missing) or "-",
        ",".join(report.applied) or "-",
        ",".join(report.failed) or "-",
        report.converted_usdt,
        time.perf_counter() - started,
    )


# --------------------------------------------------------------------------- 生命周期

settings = get_settings()
# arq 的 CLI 只配置自己的 logger，root 会停在 WARNING；不显式配一遍，
# 业务代码里的 INFO 日志（订阅执行、报价、选卡、任务开始/结束）全都不会输出。
configure_logging(settings.log_level)


def _cron_jobs(current: Settings) -> list:
    jobs: list = []
    if current.subscription_worker_enabled:
        jobs.append(_interval_cron(publish_outbox, current.arq_dispatch_seconds))
    if current.card_stock_enabled:
        jobs.append(_interval_cron(card_stock_scan, current.card_stock_interval_seconds))
    return jobs


async def startup(ctx) -> None:
    """建 AiLink/BYOC/回调客户端，放在 ctx 里复用连接池和限流窗口。"""
    current: Settings = ctx["settings"]

    ailink = None
    if current.ailink_configured:
        try:
            ailink = AiLinkClient.from_settings(
                current,
                limiter=RateLimiter("ailink", current.ailink_rate_limit_per_minute),
            )
        except Exception:
            logger.exception("初始化 AiLink 客户端失败，补卡和卡库存不会执行")

    byoc = None
    callback_http = None
    if current.subscription_worker_enabled:
        if not current.byoc_configured:
            logger.error("未配置 BYOC_API_KEY，订阅任务不会执行")
        elif not current.task_payload_configured:
            logger.error("未配置 TASK_PAYLOAD_KEY，订阅任务不会执行")
        else:
            try:
                byoc = ByocClient.from_settings(
                    current,
                    limiter=RateLimiter("byoc", current.byoc_rate_limit_per_minute),
                )
                callback_http = httpx.AsyncClient(timeout=10.0)
            except Exception:
                logger.exception("初始化 BYOC 客户端失败，订阅任务不会执行")
                byoc = None
                callback_http = None

    ctx["ailink"] = ailink
    ctx["byoc"] = byoc
    ctx["callback_http"] = callback_http
    if current.card_stock_enabled and ailink is None:
        logger.error("卡库存扫描已开启但 AiLink 客户端不可用，定时任务只会打日志")
    logger.info(
        "arq worker 启动：队列=%s 并发=%d 卡库存=%s 订阅=%s AiLink=%s",
        current.arq_queue_name,
        current.arq_max_jobs,
        current.card_stock_enabled,
        current.subscription_worker_enabled,
        ailink is not None,
    )


async def shutdown(ctx) -> None:
    for name in ("ailink", "byoc"):
        client = ctx.get(name)
        if client is not None:
            await client.aclose()
    callback_http = ctx.get("callback_http")
    if callback_http is not None:
        await callback_http.aclose()
    logger.info("arq worker 停止")


class WorkerSettings:
    """`arq app.jobs.arq_worker.WorkerSettings` 的入口。"""

    functions = [
        # 重试由数据库里的 outbox 决定，arq 不要再自己重投
        func(run_task, max_tries=1),
        func(deliver_task_callback, max_tries=1),
    ]
    cron_jobs = _cron_jobs(settings)
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = settings.arq_queue_name
    max_jobs = settings.arq_max_jobs
    job_timeout = settings.arq_job_timeout
    keep_result = 0
    health_check_interval = settings.arq_health_check_interval
    # 收到 SIGTERM 后给手上的任务留时间跑完
    job_completion_wait = 30
    ctx = {"session_factory": session_scope, "settings": settings}
