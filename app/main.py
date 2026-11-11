"""应用入口：uvicorn app.main:app --reload"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from decimal import Decimal

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import session_scope
from app.integrations.ailink.client import AiLinkClient
from app.integrations.ailink.handlers import dispatcher
from app.integrations.byoc.client import ByocClient
from app.integrations.ratelimit import RateLimiter
from app.internal import admin
from app.jobs.card_stock import run_forever as run_card_stock_job
from app.jobs.task_worker import run_forever as run_task_worker
from app.migrations import upgrade_to_head
from app.routers import subscriptions, webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.auto_migrate:
        upgrade_to_head()

    # Webhook 处理器要用会话，但集成层不直接依赖数据库，由这里注入
    dispatcher.set_session_scope(session_scope)

    stock_task: asyncio.Task[None] | None = None
    worker_task: asyncio.Task[None] | None = None
    byoc_client: ByocClient | None = None
    callback_http: httpx.AsyncClient | None = None
    ailink_client: AiLinkClient | None = None

    # 两个任务共用一个 AiLink 客户端和同一个限流窗口，避免各自一套配额叠加。
    # 配置错误只停掉依赖它的任务，API 本身要能起来，否则会变成重启循环。
    if settings.ailink_configured and (
        settings.card_stock_enabled or settings.subscription_worker_enabled
    ):
        try:
            ailink_client = AiLinkClient.from_settings(
                settings,
                limiter=RateLimiter("ailink", settings.ailink_rate_limit_per_minute),
            )
        except Exception:
            logger.exception("初始化 AiLink 客户端失败，卡库存和补卡任务不会启动")
            ailink_client = None

    if settings.card_stock_enabled:
        if ailink_client is None:
            logger.error(
                "CARD_STOCK_ENABLED=true 但未配置 AILINK_API_KEY/AILINK_API_SECRET，任务不启动"
            )
        else:
            stock_task = asyncio.create_task(
                run_card_stock_job(
                    client=ailink_client,
                    session_factory=session_scope,
                    interval_seconds=settings.card_stock_interval_seconds,
                    usdt_rate=Decimal(str(settings.card_stock_usdt_usd_rate)),
                )
            )
            logger.info("卡库存扫描任务已启动，间隔 %ss", settings.card_stock_interval_seconds)

    if settings.subscription_worker_enabled:
        if not settings.byoc_configured:
            logger.error("SUBSCRIPTION_WORKER_ENABLED=true 但未配置 BYOC_API_KEY，worker 不启动")
        elif not settings.task_payload_configured:
            logger.error(
                "SUBSCRIPTION_WORKER_ENABLED=true 但未配置 TASK_PAYLOAD_KEY，worker 不启动"
            )
        else:
            try:
                byoc_client = ByocClient.from_settings(
                    settings,
                    limiter=RateLimiter("byoc", settings.byoc_rate_limit_per_minute),
                )
                callback_http = httpx.AsyncClient(timeout=10.0)
            except Exception:
                logger.exception("初始化 BYOC 客户端失败，订阅 worker 不会启动")
                byoc_client = None
                callback_http = None
            if byoc_client is not None:
                if ailink_client is None:
                    logger.warning("未配置 AiLink，订阅任务触发的补卡会一直等待")
                worker_task = asyncio.create_task(
                    run_task_worker(
                        session_factory=session_scope,
                        settings=settings,
                        byoc=byoc_client,
                        ailink=ailink_client,
                        callback_http=callback_http,
                        interval_seconds=settings.subscription_worker_interval_seconds,
                    )
                )
                logger.info(
                    "订阅任务 worker 已启动，间隔 %ss",
                    settings.subscription_worker_interval_seconds,
                )

    logger.info(
        "应用启动完成：卡库存=%s 订阅 worker=%s",
        stock_task is not None,
        worker_task is not None,
    )

    try:
        yield
    finally:
        if stock_task is not None:
            stock_task.cancel()
            with suppress(asyncio.CancelledError):
                await stock_task
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
        if ailink_client is not None:
            await ailink_client.aclose()
        if byoc_client is not None:
            await byoc_client.aclose()
        if callback_http is not None:
            await callback_http.aclose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(subscriptions.router, prefix=settings.api_prefix)
# Webhook 地址是填到 VCC 后台的，不挂在可配置的 api_prefix 下，避免改前缀时丢事件
app.include_router(webhooks.router)
app.include_router(admin.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
