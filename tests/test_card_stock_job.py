"""定时任务外壳：租约、异常隔离、取消，以及 lifespan 的启动/关闭接线。"""

import asyncio
import logging
from datetime import timedelta
from decimal import Decimal

import pytest

import app.main as main
from app import crud
from app.config import Settings
from app.jobs import card_stock as job
from app.models import utcnow
from app.services.card_stock import StockReport


async def test_run_once_reports_and_releases_lease(session_factory, monkeypatch):
    seen: list[dict] = []

    async def fake_replenish(session, client, **kwargs):
        seen.append(kwargs)
        return StockReport(missing=["plus"], applied=["plus"])

    monkeypatch.setattr(job, "replenish", fake_replenish)

    report = await job.run_once(
        client=object(),
        session_factory=session_factory,
        usdt_rate=Decimal("0.997"),
    )

    assert report is not None
    assert report.applied == ["plus"]
    assert seen and seen[0]["usdt_rate"] == Decimal("0.997")
    with session_factory() as session:
        assert crud.acquire_job_lease(
            session,
            job.LEASE_NAME,
            locked_until=utcnow() + timedelta(minutes=5),
        )


async def test_run_once_skips_when_other_replica_holds_lease(session_factory, monkeypatch):
    called: list[int] = []

    async def fake_replenish(*args, **kwargs):
        called.append(1)
        return StockReport()

    monkeypatch.setattr(job, "replenish", fake_replenish)
    with session_factory() as session:
        crud.ensure_job_lease(session, job.LEASE_NAME)
        assert crud.acquire_job_lease(
            session,
            job.LEASE_NAME,
            locked_until=utcnow() + timedelta(minutes=5),
        )

    report = await job.run_once(
        client=object(),
        session_factory=session_factory,
        usdt_rate=Decimal("0.997"),
    )

    assert report is None
    assert called == []


async def test_run_once_releases_lease_on_error(session_factory, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("网络挂了")

    monkeypatch.setattr(job, "replenish", boom)

    with pytest.raises(RuntimeError):
        await job.run_once(
            client=object(),
            session_factory=session_factory,
            usdt_rate=Decimal("0.997"),
        )

    with session_factory() as session:
        assert crud.acquire_job_lease(
            session,
            job.LEASE_NAME,
            locked_until=utcnow() + timedelta(minutes=5),
        )


async def test_run_forever_survives_error_and_stops_on_cancel(monkeypatch):
    calls: list[int] = []

    async def fake_once(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("第一轮失败")

    monkeypatch.setattr(job, "run_once", fake_once)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await job.run_forever(
            client=object(),
            session_factory=lambda: None,
            interval_seconds=7,
            usdt_rate=Decimal("0.997"),
            sleep=fake_sleep,
        )

    assert len(calls) == 2
    assert sleeps == [7, 7]


async def test_lifespan_does_not_start_job_when_disabled(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        Settings(_env_file=None, auto_migrate=False, card_stock_enabled=False),
    )
    created: list[int] = []

    class FakeClient:
        @classmethod
        def from_settings(cls, settings, *, limiter=None):
            created.append(1)
            return cls()

    monkeypatch.setattr(main, "AiLinkClient", FakeClient)

    async with main.lifespan(None):
        pass

    assert created == []


async def test_lifespan_starts_job_and_closes_client(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            _env_file=None,
            auto_migrate=False,
            card_stock_enabled=True,
            ailink_api_key="ak_test",
            ailink_api_secret="sk_test",
            card_stock_interval_seconds=60,
            card_stock_usdt_usd_rate=0.997,
        ),
    )
    events: list[object] = []

    class FakeClient:
        @classmethod
        def from_settings(cls, settings, *, limiter=None):
            events.append("created")
            return cls()

        async def aclose(self):
            events.append("closed")

    started = asyncio.Event()
    cancelled: list[bool] = []

    async def fake_job(**kwargs):
        events.append(("interval", kwargs["interval_seconds"]))
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(main, "AiLinkClient", FakeClient)
    monkeypatch.setattr(main, "run_card_stock_job", fake_job)

    async with main.lifespan(None):
        await asyncio.wait_for(started.wait(), 1)

    assert events[0] == "created"
    assert ("interval", 60) in events
    assert events[-1] == "closed"
    assert cancelled == [True]


async def test_lifespan_without_credentials_logs_error(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.main")
    monkeypatch.setattr(
        main,
        "settings",
        Settings(_env_file=None, auto_migrate=False, card_stock_enabled=True),
    )
    created: list[int] = []

    class FakeClient:
        @classmethod
        def from_settings(cls, settings, *, limiter=None):
            created.append(1)
            return cls()

    monkeypatch.setattr(main, "AiLinkClient", FakeClient)

    async with main.lifespan(None):
        pass

    assert created == []
    assert "未配置 AILINK_API_KEY" in caplog.text


async def test_lifespan_starts_subscription_worker_and_closes_clients(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            _env_file=None,
            auto_migrate=False,
            subscription_worker_enabled=True,
            byoc_api_key="byoc_key",
            task_payload_key="payload_key",
            subscription_worker_interval_seconds=5,
        ),
    )
    events: list[str] = []

    class FakeByoc:
        @classmethod
        def from_settings(cls, settings, *, limiter=None):
            events.append("byoc_created")
            return cls()

        async def aclose(self):
            events.append("byoc_closed")

    started = asyncio.Event()

    async def fake_worker(**kwargs):
        events.append(f"worker:{kwargs['interval_seconds']}")
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("worker_cancelled")
            raise

    monkeypatch.setattr(main, "ByocClient", FakeByoc)
    monkeypatch.setattr(main, "run_task_worker", fake_worker)

    async with main.lifespan(None):
        await asyncio.wait_for(started.wait(), 1)

    assert "byoc_created" in events
    assert "worker:5" in events
    assert "worker_cancelled" in events
    assert events[-1] == "byoc_closed"


async def test_lifespan_subscription_worker_without_byoc_key_logs_error(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.main")
    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            _env_file=None,
            auto_migrate=False,
            subscription_worker_enabled=True,
            task_payload_key="payload_key",
        ),
    )
    created: list[int] = []

    class FakeByoc:
        @classmethod
        def from_settings(cls, settings, *, limiter=None):
            created.append(1)
            return cls()

    monkeypatch.setattr(main, "ByocClient", FakeByoc)

    async with main.lifespan(None):
        pass

    assert created == []
    assert "未配置 BYOC_API_KEY" in caplog.text


def test_empty_secret_is_not_treated_as_configured():
    settings = Settings(
        _env_file=None,
        ailink_api_key="",
        ailink_api_secret="  ",
        byoc_api_key="",
        task_payload_key=" ",
    )

    assert settings.ailink_configured is False
    assert settings.byoc_configured is False
    assert settings.task_payload_configured is False


async def test_lifespan_blank_byoc_key_keeps_api_up(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.main")
    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            _env_file=None,
            auto_migrate=False,
            subscription_worker_enabled=True,
            byoc_api_key="",
            task_payload_key="payload_key",
        ),
    )
    started: list[int] = []

    async def fake_worker(**kwargs):
        started.append(1)

    monkeypatch.setattr(main, "run_task_worker", fake_worker)

    async with main.lifespan(None):
        pass

    assert started == []
    assert "未配置 BYOC_API_KEY" in caplog.text


async def test_lifespan_invalid_byoc_url_keeps_api_up(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.main")
    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            _env_file=None,
            auto_migrate=False,
            subscription_worker_enabled=True,
            byoc_api_key="key",
            byoc_base_url="http://wangbohui.com",
            task_payload_key="payload_key",
        ),
    )
    started: list[int] = []

    async def fake_worker(**kwargs):
        started.append(1)

    monkeypatch.setattr(main, "run_task_worker", fake_worker)

    async with main.lifespan(None):
        pass

    assert started == []
    assert "初始化 BYOC 客户端失败" in caplog.text
    assert "base_url 必须是本站 HTTPS 地址" in caplog.text


async def test_lifespan_zero_rate_limit_keeps_api_up(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.main")
    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            _env_file=None,
            auto_migrate=False,
            subscription_worker_enabled=True,
            byoc_api_key="key",
            task_payload_key="payload_key",
            byoc_rate_limit_per_minute=0,
        ),
    )
    started: list[int] = []

    async def fake_worker(**kwargs):
        started.append(1)

    monkeypatch.setattr(main, "run_task_worker", fake_worker)

    async with main.lifespan(None):
        pass

    assert started == []
    assert "初始化 BYOC 客户端失败" in caplog.text
