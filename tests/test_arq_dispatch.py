"""outbox → arq 的投递层：延迟、去重、ack、失败重试，以及 actor 的防重入。"""

from datetime import datetime, timedelta

from arq.connections import RedisSettings

from app import crud
from app.jobs import arq_worker
from app.models import utcnow
from tests.factories import FakeRedis, drain_queue, make_ctx, schedule_run, task_outbox
from tests.test_subscriptions import make_settings


def open_card_task(
    session,
    *,
    plan="plus",
    request_id="order-202611170000000001",
    run_at=None,
):
    """建任务时自带一条 run 调度（token=1）。"""
    return crud.create_task(
        session,
        kind="open_card",
        plan=plan,
        request_id=request_id,
        run_at=run_at,
    )


def after_now() -> datetime:
    """任务创建之后的“当前时间”。"""
    return utcnow() + timedelta(seconds=1)


async def test_publish_enqueues_outbox_with_stable_job_id(db_session, session_factory):
    now = after_now()
    task = open_card_task(db_session, run_at=now)
    row = task_outbox(db_session, task.id or 0, "run")[-1]
    ctx = make_ctx(make_settings(), session_factory)

    published = await arq_worker.publish_outbox(ctx, now=now)

    assert published == 1
    assert ctx["redis"].jobs == [
        (
            "run_task",
            (task.id, row.token, row.id),
            {"_job_id": f"kt:run:{row.id}", "_defer_by": None},
        )
    ]


async def test_publish_turns_run_at_into_defer_by(db_session, session_factory):
    now = after_now()
    open_card_task(db_session, run_at=now + timedelta(seconds=120))
    ctx = make_ctx(make_settings(), session_factory)

    await arq_worker.publish_outbox(ctx, now=now)

    (_name, _args, kwargs) = ctx["redis"].jobs[0]
    assert kwargs["_defer_by"] == 120


async def test_publish_keeps_row_until_worker_acks(db_session, session_factory):
    now = after_now()
    task = open_card_task(db_session, run_at=now)
    ctx = make_ctx(make_settings(), session_factory)

    await arq_worker.publish_outbox(ctx, now=now)
    await arq_worker.publish_outbox(ctx, now=now)

    # 行还在（等 actor ack），但 arq 侧同一个 _job_id 只留一条
    assert len(task_outbox(db_session, task.id or 0, "run")) == 1
    assert len(ctx["redis"].jobs) == 1


async def test_publish_keeps_row_when_redis_fails(db_session, session_factory):
    now = after_now()
    task = open_card_task(db_session, run_at=now)
    redis = FakeRedis()
    redis.fail_next = True
    ctx = make_ctx(make_settings(), session_factory, redis=redis)

    assert await arq_worker.publish_outbox(ctx, now=now) == 0
    assert redis.jobs == []
    assert len(task_outbox(db_session, task.id or 0, "run")) == 1

    # 下一轮自动补投
    assert await arq_worker.publish_outbox(ctx, now=now) == 1
    assert len(redis.jobs) == 1


async def test_publish_sends_multiple_rows(db_session, session_factory):
    now = after_now()
    tasks = [
        crud.create_task(
            db_session,
            kind="open_card",
            plan=plan,
            request_id=f"order-20261117000000010{index}",
            run_at=now,
        )
        for index, plan in enumerate(("plus", "pro100", "pro200"))
    ]
    ctx = make_ctx(make_settings(), session_factory)

    assert await arq_worker.publish_outbox(ctx, now=now) == 3
    assert [args[0] for _name, args, _kwargs in ctx["redis"].jobs] == [task.id for task in tasks]


async def test_publish_leaves_subscription_runs_when_byoc_missing(db_session, session_factory):
    """BYOC 没配置时订阅任务不投递；已产生的回调照常发。"""
    now = after_now()
    subscription = crud.create_task(
        db_session,
        kind="open_subscription",
        plan="plus",
        request_id="order-202611170000000020",
        run_at=now,
    )
    crud.save_task_with_outbox(
        db_session,
        subscription,
        kind=crud.OUTBOX_CALLBACK,
        run_at=now,
    )
    card_task = open_card_task(db_session, request_id="order-202611170000000021", run_at=now)
    ctx = make_ctx(make_settings(), session_factory, byoc=None)

    await arq_worker.publish_outbox(ctx, now=now)

    published = {name for name, _args, _kwargs in ctx["redis"].jobs}
    assert published == {"run_task", "deliver_task_callback"}
    assert [args[0] for name, args, _kw in ctx["redis"].jobs if name == "run_task"] == [
        card_task.id
    ]


async def test_run_task_acks_outbox_and_runs(db_session, session_factory):
    now = after_now()
    task = open_card_task(db_session, request_id="order-202611170000000030", run_at=now)
    row = task_outbox(db_session, task.id or 0, "run")[-1]
    ctx = make_ctx(make_settings(), session_factory, ailink=None, byoc=None)

    await arq_worker.run_task(ctx, task.id or 0, row.token or 0, row.id or 0, now=now)

    assert task_outbox(db_session, task.id or 0, "run") == []


async def test_run_task_skips_terminal_task_on_redelivery(db_session, session_factory):
    now = after_now()
    task = open_card_task(db_session, request_id="order-202611170000000031", run_at=now)
    task.status = "succeeded"
    crud.save_task(db_session, task)
    row = task_outbox(db_session, task.id or 0, "run")[-1]
    ctx = make_ctx(make_settings(), session_factory, ailink=None, byoc=None)

    await arq_worker.run_task(ctx, task.id or 0, row.token or 0, row.id or 0, now=now)

    db_session.refresh(task)
    assert task.status == "succeeded"
    assert task_outbox(db_session, task.id or 0, "run") == []


async def test_run_task_drops_message_from_stale_token(db_session, session_factory):
    now = after_now()
    task = open_card_task(db_session, request_id="order-202611170000000032", run_at=now)
    old = task_outbox(db_session, task.id or 0, "run")[-1]
    # 任务又被排了一次，旧的令牌就失效了
    schedule_run(db_session, task, run_at=now)
    ctx = make_ctx(make_settings(), session_factory, ailink=None, byoc=None)

    await arq_worker.run_task(ctx, task.id or 0, old.token or 0, old.id or 0, now=now)

    db_session.refresh(task)
    assert task.status == "pending"
    assert len(task_outbox(db_session, task.id or 0, "run")) == 1  # 新那条没被动


async def test_run_task_requeues_after_unexpected_error(db_session, session_factory, monkeypatch):
    now = after_now()
    task = open_card_task(db_session, request_id="order-202611170000000033", run_at=now)
    row = task_outbox(db_session, task.id or 0, "run")[-1]
    ctx = make_ctx(make_settings(), session_factory, ailink=None, byoc=None)

    async def boom(*args, **kwargs):
        raise RuntimeError("数据库抖动")

    monkeypatch.setattr(arq_worker, "execute_task", boom)

    await arq_worker.run_task(ctx, task.id or 0, row.token or 0, row.id or 0, now=now)

    db_session.refresh(task)
    assert task.status == "pending"
    assert "数据库抖动" in (task.last_error or "")
    rows = task_outbox(db_session, task.id or 0, "run")
    assert len(rows) == 1
    assert rows[0].run_at == now + timedelta(seconds=arq_worker.ERROR_RETRY_SECONDS)
    assert rows[0].token == task.attempts


async def test_callback_actor_acks_outbox_and_is_idempotent(db_session, session_factory):
    now = after_now()
    task = open_card_task(db_session, request_id="order-202611170000000034")
    task.callback_url = "https://merchant.example/cb"
    task.callback_delivered = True
    crud.save_task_with_outbox(db_session, task, kind=crud.OUTBOX_CALLBACK, run_at=now)
    row = task_outbox(db_session, task.id or 0, "callback")[-1]
    ctx = make_ctx(make_settings(), session_factory, callback_http=None)

    await arq_worker.deliver_task_callback(ctx, task.id or 0, row.id or 0, now=now)

    assert task_outbox(db_session, task.id or 0, "callback") == []


async def test_callback_keeps_row_when_delivery_raises(db_session, session_factory, monkeypatch):
    """回调投递遇到没被处理的异常时不能 ack，否则这条回调就永远丢了。"""
    now = after_now()
    task = open_card_task(db_session, request_id="order-202611170000000035")
    task.callback_url = "https://merchant.example/cb"
    crud.save_task_with_outbox(db_session, task, kind=crud.OUTBOX_CALLBACK, run_at=now)
    row = task_outbox(db_session, task.id or 0, "callback")[-1]
    ctx = make_ctx(make_settings(), session_factory, callback_http=None)

    async def boom(*args, **kwargs):
        raise RuntimeError("回调客户端炸了")

    monkeypatch.setattr(arq_worker, "deliver_callback", boom)

    await arq_worker.deliver_task_callback(ctx, task.id or 0, row.id or 0, now=now)

    assert len(task_outbox(db_session, task.id or 0, "callback")) == 1


async def test_run_task_ignores_missing_task(session_factory):
    ctx = make_ctx(make_settings(), session_factory, ailink=None, byoc=None)

    await arq_worker.run_task(ctx, 99999, 1, 99999, now=after_now())  # 不抛异常


def test_worker_settings_contract():
    """arq 入口的关键配置，避免以后改动悄悄退化。"""
    names = {function.name for function in arq_worker.WorkerSettings.functions}
    assert names == {"run_task", "deliver_task_callback"}
    assert arq_worker.WorkerSettings.keep_result == 0
    assert arq_worker.WorkerSettings.max_jobs >= 2
    assert arq_worker.WorkerSettings.job_timeout > 0
    assert arq_worker.WorkerSettings.queue_name == arq_worker.settings.arq_queue_name
    assert arq_worker.WorkerSettings.redis_settings == RedisSettings.from_dsn(
        arq_worker.settings.redis_url
    )


def test_cron_jobs_are_registered_once_per_slot():
    from app.config import Settings

    jobs = arq_worker._cron_jobs(
        Settings(
            _env_file=None,
            card_stock_enabled=True,
            subscription_worker_enabled=True,
            arq_dispatch_seconds=5,
            card_stock_interval_seconds=300,
        )
    )

    assert [job.coroutine.__name__ for job in jobs] == ["publish_outbox", "card_stock_scan"]
    assert all(job.unique for job in jobs)


def test_cron_jobs_are_empty_when_disabled():
    from app.config import Settings

    jobs = arq_worker._cron_jobs(
        Settings(_env_file=None, card_stock_enabled=False, subscription_worker_enabled=False)
    )

    assert jobs == []


async def test_drain_queue_is_empty_by_default(session_factory):
    ctx = make_ctx(make_settings(), session_factory)

    assert await drain_queue(ctx) == 0
