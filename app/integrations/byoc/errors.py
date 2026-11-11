"""BYOC 错误：沿用上游约定，超时不等于失败。"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


class APIError(RuntimeError):
    def __init__(self, status, detail, *, retry_after=None, trace_id=None, response_is_json=False):
        self.status = status
        self.detail = detail
        self.code = detail.get("code") if isinstance(detail, dict) else None
        self.message = (
            detail.get("message", "请求失败") if isinstance(detail, dict) else str(detail)
        )
        self.retry_after = retry_after
        self.trace_id = trace_id
        self.response_is_json = response_is_json
        self.recovered_task_id = None
        super().__init__(f"HTTP {status}: {self.message}")

    def retry_after_seconds(self, default=1):
        try:
            value = float(self.retry_after)
            if math.isfinite(value) and value >= 0:
                return value
        except (TypeError, ValueError):
            pass
        try:
            value = parsedate_to_datetime(self.retry_after)
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return max(0.0, (value - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return default


class OutcomeUnknown(RuntimeError):
    def __init__(self, request_id, *, last_error=None):
        self.request_id = request_id
        self.trace_id = getattr(last_error, "trace_id", None)
        self.retry_after = getattr(last_error, "retry_after", None)
        self.last_status = getattr(last_error, "status", None)
        self.last_code = getattr(last_error, "code", None)
        super().__init__(f"提交结果待确认；保留原请求号 {request_id}，稍后查询原单，不能改号补单")


class PollTimeout(TimeoutError):
    def __init__(self, task_id):
        self.task_id = task_id
        super().__init__(f"查询等待时间已到；稍后继续查询原订单 {task_id}")
