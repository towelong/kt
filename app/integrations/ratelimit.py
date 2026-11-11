"""进程内固定窗口限流：跟随平台口径，每个真实 HTTP 请求消耗一个配额。

BYOC 300 次/分钟、AiLink 450 次/分钟；重试和 recover 也是请求，同样要过桶。
多副本部署时每个进程各有一份窗口，总量会按副本数叠加，扩容时改配额或换跨进程实现。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimitExceeded(RuntimeError):
    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"{name} 限流窗口已满，需等待 {retry_after:.1f}s")


class RateLimiter:
    def __init__(
        self,
        name: str,
        limit_per_minute: int,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if limit_per_minute <= 0:
            raise ValueError("limit_per_minute 必须是正整数")
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须大于 0")
        self.name = name
        self.limit = limit_per_minute
        self.window_seconds = window_seconds
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._window_start: float | None = None
        self._count = 0
        self._lock = asyncio.Lock()

    def _reset_if_needed(self, now: float) -> None:
        if self._window_start is None or now >= self._window_start + self.window_seconds:
            self._window_start = now
            self._count = 0

    def _remaining_window(self, now: float) -> float:
        assert self._window_start is not None
        return max(0.0, self._window_start + self.window_seconds - now)

    async def acquire(self, *, max_wait: float | None = None) -> None:
        """占一个配额；窗口满时等到下个窗口，超过 max_wait 抛 RateLimitExceeded。"""
        while True:
            async with self._lock:
                now = self._clock()
                self._reset_if_needed(now)
                if self._count < self.limit:
                    self._count += 1
                    return
                wait = self._remaining_window(now)
                if max_wait is not None and wait > max_wait:
                    raise RateLimitExceeded(self.name, wait)
            await self._sleep(wait)
