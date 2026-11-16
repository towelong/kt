"""全局日志格式：时间、毫秒和时区偏移都要在。"""

import logging
import re

from app.log_config import LOG_FORMAT, AppFormatter


def test_formatter_includes_time_and_offset():
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    text = AppFormatter(LOG_FORMAT).format(record)

    assert re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4} INFO\s+\[app\.test\] hello$",
        text,
    )
