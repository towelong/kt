"""应用入口：uvicorn app.main:app --reload"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.internal import admin
from app.migrations import upgrade_to_head
from app.routers import items, users, webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.auto_migrate:
        upgrade_to_head()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(items.router, prefix=settings.api_prefix)
app.include_router(users.router, prefix=settings.api_prefix)
# Webhook 地址是填到 VCC 后台的，不挂在可配置的 api_prefix 下，避免改前缀时丢事件
app.include_router(webhooks.router)
app.include_router(admin.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
