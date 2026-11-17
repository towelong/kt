"""全局日志配置：一个格式、一个 handler，应用和 uvicorn/Alembic 共用。

时间用进程本地时区并带偏移（例如 ``+0800``），和数据库里的 UTC 时间能直接对上；
需要北京时间时给容器设置 ``TZ=Asia/Shanghai`` 即可。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"

# 这些 logger 自己会装 handler（uvicorn / arq CLI），统一换成我们的格式并关掉向上传播，
# 否则同一条记录会被打印两次。
_OWNED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "arq")
_QUIET_LOGGERS = ("sqlalchemy.engine", "httpx", "httpcore")


class AppFormatter(logging.Formatter):
    """在默认 ``asctime`` 上补毫秒和时区偏移。"""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created).astimezone()
        return (
            moment.strftime("%Y-%m-%d %H:%M:%S.")
            + f"{moment.microsecond // 1000:03d}"
            + moment.strftime("%z")
        )


def configure_logging(level: str | None = None) -> None:
    """统一 root、uvicorn/arq 和安静第三方 logger；重复调用不会叠 handler。"""
    from app.config import get_settings

    resolved = (level or get_settings().log_level or "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(AppFormatter(LOG_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved)

    for name in _OWNED_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(resolved)
        logger.propagate = False

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
