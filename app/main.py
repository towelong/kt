"""应用入口：uvicorn app.main:app --reload

任务执行在独立的 arq worker 进程里：

    uv run arq app.jobs.arq_worker.WorkerSettings
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import safe_url, session_scope
from app.integrations.ailink.handlers import dispatcher
from app.internal import admin
from app.log_config import configure_logging
from app.migrations import upgrade_to_head
from app.routers import subscriptions, webhooks

logger = logging.getLogger(__name__)
settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("API 启动：数据库=%s 自动迁移=%s", safe_url(), settings.auto_migrate)
    if settings.auto_migrate:
        upgrade_to_head()

    # Webhook 处理器要用会话，但集成层不直接依赖数据库，由这里注入
    dispatcher.set_session_scope(session_scope)

    logger.info("API 启动完成；任务由独立 arq worker 执行")
    yield
    logger.info("API 已停止")


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
