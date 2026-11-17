"""以代码方式调用 Alembic：启动时自升级、管理接口触发、测试用临时库。"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Engine

from app.config import database_backend, get_settings, mask_url

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # configparser 会把 % 当插值符，URL 里的 % 要转义
    cfg.set_main_option("sqlalchemy.url", (url or get_settings().database_url).replace("%", "%%"))
    return cfg


def upgrade_to_head(url: str | None = None) -> None:
    target = url or get_settings().database_url
    # 连不上库时最容易看错的就是“到底连的哪个地址”，这里先打脱敏后的目标
    logger.info(
        "开始迁移数据库 target=%s backend=%s",
        mask_url(target),
        database_backend(target),
    )
    try:
        command.upgrade(alembic_config(target), "head")
    except Exception:
        logger.error(
            "数据库迁移失败 target=%s：检查 DATABASE_URL 的主机/端口/账号，"
            "容器里的 127.0.0.1 不是宿主机（用 host.docker.internal 或服务名）",
            mask_url(target),
        )
        raise
    logger.info("数据库迁移完成 target=%s", mask_url(target))


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision(url: str | None = None) -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()
