"""引擎与会话：SQLite / MySQL 的差异只在这里出现一次。"""

from collections.abc import Iterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlmodel import Session, create_engine

from app.config import get_settings, mask_url

settings = get_settings()


def _engine_kwargs(url: str) -> dict[str, Any]:
    if url.startswith("sqlite"):
        # FastAPI 的同步端点跑在线程池里，必须放开线程检查
        return {"connect_args": {"check_same_thread": False}}

    # MySQL：连接可能被服务端回收，用 pre_ping + recycle 兜住
    return {
        "pool_pre_ping": True,
        "pool_recycle": settings.pool_recycle_seconds,
        "pool_size": settings.pool_size,
        "max_overflow": settings.max_overflow,
    }


engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    **_engine_kwargs(settings.database_url),
)


if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        # SQLite 默认不校验外键，MySQL 默认校验，这里对齐
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def safe_url(url: str | URL | None = None) -> str:
    """打日志用，绝不带出凭据。"""
    return mask_url(url or engine.url)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def engine_info() -> dict[str, Any]:
    return {"dialect": engine.dialect.name, "url": safe_url()}


__all__ = ["engine", "engine_info", "get_session", "safe_url"]
