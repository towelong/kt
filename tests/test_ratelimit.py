"""进程内固定窗口限流：边界、窗口重置、等待上限、并发。"""

import asyncio

import pytest

from app.integrations.ratelimit import RateLimiter, RateLimitExceeded


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_limiter(limit: int, clock: FakeClock | None = None) -> tuple[RateLimiter, FakeClock]:
    clock = clock or FakeClock()
    return (
        RateLimiter("test", limit, clock=clock.clock, sleep=clock.sleep),
        clock,
    )


async def test_allows_exactly_limit_requests():
    limiter, clock = make_limiter(3)

    for _ in range(3):
        await limiter.acquire()

    assert clock.sleeps == []


async def test_waits_until_next_window_when_full():
    limiter, clock = make_limiter(2)
    await limiter.acquire()
    await limiter.acquire()

    await limiter.acquire()

    assert clock.sleeps == [60.0]
    assert clock.now == 1060.0


async def test_window_resets_after_minute():
    limiter, clock = make_limiter(1)
    await limiter.acquire()

    clock.now += 61
    await limiter.acquire()

    assert clock.sleeps == []


async def test_max_wait_raises_instead_of_blocking_forever():
    limiter, clock = make_limiter(1)
    await limiter.acquire()

    with pytest.raises(RateLimitExceeded) as info:
        await limiter.acquire(max_wait=10)

    assert info.value.name == "test"
    assert info.value.retry_after == 60.0
    assert clock.sleeps == []


async def test_concurrent_acquires_do_not_exceed_limit():
    limiter, clock = make_limiter(2)

    await asyncio.gather(*(limiter.acquire() for _ in range(5)))

    assert sum(clock.sleeps) == 120.0  # 两批等待，每批到下一个窗口
    assert clock.now >= 1120.0


def test_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        RateLimiter("bad", 0)
    with pytest.raises(ValueError):
        RateLimiter("bad", 10, window_seconds=0)
