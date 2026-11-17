"""任务执行器：按 kind 分发到订阅/补卡流程，不关心是谁调度它。

arq actor、测试、将来的脚本都调这里，业务逻辑仍然在 `app/services` 里。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session

from app import crud
from app.config import Settings
from app.models import Task, utcnow
from app.services.subscriptions import (
    KIND_OPEN_CARD,
    KIND_SUBSCRIPTION,
    STATUS_FAILED,
    process_open_card_task,
    process_subscription_task,
)

logger = logging.getLogger(__name__)


async def execute_task(
    session: Session,
    task: Task,
    *,
    byoc,
    ailink,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """执行一条任务；状态推进、重试排期都由 services 里的状态机写回数据库。"""
    now = now or utcnow()
    if task.kind == KIND_OPEN_CARD:
        await process_open_card_task(session, task, ailink=ailink, settings=settings, now=now)
        return
    if task.kind == KIND_SUBSCRIPTION:
        if byoc is None:
            # 配置缺失不是“等卡”，重试也不会自己好，直接失败并留原因
            task.status = STATUS_FAILED
            task.last_error = "未配置 BYOC，无法执行订阅任务"
            crud.save_task(session, task)
            return
        await process_subscription_task(session, task, byoc=byoc, settings=settings, now=now)
        return

    task.status = STATUS_FAILED
    task.last_error = f"未知任务类型：{task.kind}"
    crud.save_task(session, task)
    logger.error("未知任务类型 task=%s kind=%s", task.id, task.kind)


def log_queue_status(session: Session, *, now: datetime | None = None) -> None:
    """打一份任务队列快照，空闲时也能看到 worker 还活着。"""
    counts = crud.task_status_counts(session)
    summary = " ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "空"
    logger.info("任务队列快照 %s 待投递=%d", summary, crud.pending_outbox_count(session))
