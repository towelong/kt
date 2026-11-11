"""Alembic 环境：连接串与元数据都取自应用本身，两种库共用同一套迁移。"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection, make_url
from sqlmodel import SQLModel
from sqlmodel.sql.sqltypes import AutoString, UTCDateTime

import app.models  # noqa: F401  注册所有表
from app.config import database_backend, get_settings, mask_url

config = context.config

if config.config_file_name is not None:
    # 默认 disable_existing_loggers=True 会把 uvicorn/app 的 logger 全关掉，
    # 启动失败时连堆栈都打不出来，所以应用内调用迁移时必须保留现有 logger。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# autogenerate 的比较基准
target_metadata = SQLModel.metadata

# 优先用调用方显式传入的 URL（测试、脚本），否则读 DATABASE_URL
url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
database_backend(url)  # 尽早对不支持的 URL 报错
is_sqlite = url.startswith("sqlite")


def render_item(type_, obj, autogen_context):
    """SQLModel 自有类型落成标准 sa 类型，迁移脚本不依赖 sqlmodel 内部实现。"""
    if type_ == "type":
        if isinstance(obj, AutoString):
            return f"sa.String(length={obj.length!r})"
        if isinstance(obj, UTCDateTime):
            return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # SQLite 不支持多数 ALTER，batch 模式会重建表；MySQL 不需要
        render_as_batch=is_sqlite,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # 迁移是一次性任务，用 NullPool，别把应用连接池带进来
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


print(f"[alembic] target = {mask_url(make_url(url))} ({database_backend(url)})")

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
