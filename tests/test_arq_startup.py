"""arq worker 的启动/关闭接线：客户端复用、配置缺失只停依赖它的能力。

对应原来的 tests/test_card_stock_job.py：进程内循环和 job_lease 没了，
这些场景改成验证 `startup` / `shutdown` 和 cron 注册。
"""

import asyncio
import logging

import httpx

import app.main as main
from app.config import Settings
from app.jobs import arq_worker


def make_worker_settings(**overrides) -> Settings:
    defaults = {"_env_file": None, "auto_migrate": False}
    defaults.update(overrides)
    return Settings(**defaults)


async def test_startup_creates_clients_and_shutdown_closes_them(monkeypatch):
    settings = make_worker_settings(
        subscription_worker_enabled=True,
        byoc_api_key="byoc_key",
        task_payload_key="payload_key",
        ailink_api_key="ak_test",
        ailink_api_secret="sk_test",
    )
    events: list[str] = []

    class FakeAiLink:
        @classmethod
        def from_settings(cls, settings, *, limiter=None):
            events.append("ailink_created")
            return cls()

        async def aclose(self):
            events.append("ailink_closed")

    class FakeByoc:
        @classmethod
        def from_settings(cls, settings, *, limiter=None):
            events.append("byoc_created")
            return cls()

        async def aclose(self):
            events.append("byoc_closed")

    monkeypatch.setattr(arq_worker, "AiLinkClient", FakeAiLink)
    monkeypatch.setattr(arq_worker, "ByocClient", FakeByoc)
    ctx = {"settings": settings, "session_factory": None}

    await arq_worker.startup(ctx)

    assert events[:2] == ["ailink_created", "byoc_created"]
    assert isinstance(ctx["callback_http"], httpx.AsyncClient)

    await arq_worker.shutdown(ctx)

    assert events[-2:] == ["ailink_closed", "byoc_closed"]
    assert ctx["callback_http"].is_closed


async def test_startup_without_byoc_key_logs_error(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.jobs.arq_worker")
    settings = make_worker_settings(
        subscription_worker_enabled=True,
        task_payload_key="payload_key",
        ailink_api_key="ak_test",
        ailink_api_secret="sk_test",
    )
    created: list[int] = []

    class FakeByoc:
        @classmethod
        def from_settings(cls, settings, *, limiter=None):
            created.append(1)
            return cls()

    monkeypatch.setattr(arq_worker, "ByocClient", FakeByoc)
    ctx = {"settings": settings, "session_factory": None}

    await arq_worker.startup(ctx)

    assert created == []
    assert ctx["byoc"] is None
    assert "未配置 BYOC_API_KEY" in caplog.text


async def test_startup_invalid_byoc_url_keeps_running(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.jobs.arq_worker")
    settings = make_worker_settings(
        subscription_worker_enabled=True,
        byoc_api_key="key",
        byoc_base_url="http://wangbohui.com",
        task_payload_key="payload_key",
    )
    ctx = {"settings": settings, "session_factory": None}

    await arq_worker.startup(ctx)

    assert ctx["byoc"] is None
    assert "初始化 BYOC 客户端失败" in caplog.text
    assert "base_url 必须是本站 HTTPS 地址" in caplog.text


async def test_startup_zero_rate_limit_keeps_running(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.jobs.arq_worker")
    settings = make_worker_settings(
        subscription_worker_enabled=True,
        byoc_api_key="key",
        task_payload_key="payload_key",
        byoc_rate_limit_per_minute=0,
    )
    ctx = {"settings": settings, "session_factory": None}

    await arq_worker.startup(ctx)

    assert ctx["byoc"] is None
    assert "初始化 BYOC 客户端失败" in caplog.text


async def test_startup_warns_when_card_stock_has_no_ailink(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.jobs.arq_worker")
    settings = make_worker_settings(card_stock_enabled=True)
    ctx = {"settings": settings, "session_factory": None}

    await arq_worker.startup(ctx)

    assert ctx["ailink"] is None
    assert "卡库存扫描已开启但 AiLink 客户端不可用" in caplog.text


async def test_api_lifespan_starts_no_in_process_jobs(monkeypatch):
    """任务执行搬到独立 worker 进程，API 的 lifespan 不再起任何循环。"""
    monkeypatch.setattr(main, "settings", make_worker_settings(auto_migrate=False))
    started: list[int] = []

    async def fake_task(*args, **kwargs):
        started.append(1)

    monkeypatch.setattr(asyncio, "create_task", fake_task)

    async with main.lifespan(None):
        pass

    assert started == []
    assert not hasattr(main, "run_task_worker")
    assert not hasattr(main, "run_card_stock_job")


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


def test_worker_process_configures_app_logging():
    """worker 是独立进程，必须自己配 root；否则业务 INFO 日志全被丢掉。"""
    import logging

    from app.jobs import arq_worker
    from app.log_config import AppFormatter

    root = logging.getLogger()
    assert root.level == getattr(logging, arq_worker.settings.log_level.upper())
    assert any(isinstance(handler.formatter, AppFormatter) for handler in root.handlers)
