"""数据访问层：只碰 Session 和模型，不碰 HTTP。分页统一 offset/limit。"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.models import (
    ApiKey,
    Card,
    CardOperation,
    CardTransaction,
    Task,
    TaskOutbox,
    utcnow,
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class ConflictError(Exception):
    """唯一约束冲突，由路由层翻译成 409，不依赖数据库错误码。"""


class NotFoundError(Exception):
    """资源不存在，由路由层翻译成 404。"""


# ---------- Card ----------


def create_card_intent(
    session: Session,
    *,
    plan: str,
    request_no: str,
) -> Card:
    """先落占位行再调申请卡：超时后能靠 request_no 认出“已提交过”，不重复开卡。

    余额从 0 开始：首充是独立异步流程，只认 CARD_OPERATION.RECHARGE_SUCCESS 的入账。
    """
    card = Card(
        plan=plan,
        request_no=request_no,
        balance=Decimal("0.00"),
        apply_status="PENDING",
        enabled=False,
    )
    session.add(card)
    session.commit()
    session.refresh(card)
    return card


def get_card_by_request_no(
    session: Session,
    request_no: str,
    *,
    for_update: bool = False,
) -> Card | None:
    stmt = select(Card).where(Card.request_no == request_no)
    if for_update:
        stmt = stmt.with_for_update()
    return session.exec(stmt).first()


def get_card_by_platform_id(
    session: Session,
    card_id: str,
    *,
    for_update: bool = False,
) -> Card | None:
    stmt = select(Card).where(Card.card_id == card_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.exec(stmt).first()


def get_card(session: Session, card_id: int) -> Card:
    card = session.get(Card, card_id)
    if card is None:
        raise NotFoundError(f"card {card_id} 不存在")
    return card


def find_usable_card(
    session: Session,
    plan: str,
    *,
    min_balance: Decimal | None = None,
) -> Card | None:
    """可用 = 已启用、次数没用完；传 min_balance 时要求余额不低于它。"""
    stmt = select(Card).where(
        Card.plan == plan,
        Card.enabled.is_(True),
        Card.used_count < 3,
    )
    if min_balance is not None:
        stmt = stmt.where(Card.balance >= min_balance)
    return session.exec(stmt.order_by(col(Card.id))).first()


def get_recent_incomplete_card(session: Session, plan: str, since: datetime) -> Card | None:
    """近期提交、但还没变成可用卡（等三要素或首充）的占位，避免重复开卡。

    首充成功前卡是停用的，也算在途；次数用完被停用的卡不算，应该被下一轮补掉。
    已经明确失败的占位（开卡被拒 apply_status=FAILED、首充失败）不会再变成可用卡，
    不能算在途：它们没有卡号，之前会被下面的 card_no IS NULL 一直挡着，
    导致后续每一轮补卡都空转，而补卡任务的重试次数还在照常消耗。
    """
    stmt = (
        select(Card)
        .where(
            Card.plan == plan,
            Card.enabled.is_(False),
            Card.created_at >= since,
            or_(Card.apply_status.is_(None), Card.apply_status != "FAILED"),
            or_(Card.initial_funding_status.is_(None), Card.initial_funding_status != "FAILED"),
            or_(
                Card.apply_status == "PENDING",
                Card.card_no.is_(None),
                Card.initial_funding_status == "PENDING",
            ),
        )
        .order_by(col(Card.id))
    )
    return session.exec(stmt).first()


def save_card(session: Session, card: Card) -> Card:
    card.updated_at = utcnow()
    session.add(card)
    session.commit()
    session.refresh(card)
    return card


def mark_card_failed(session: Session, card: Card) -> Card:
    card.apply_status = "FAILED"
    card.enabled = False
    return save_card(session, card)


# ---------- CardTransaction ----------


def get_card_transaction(
    session: Session,
    transaction_id: str,
    *,
    for_update: bool = False,
) -> CardTransaction | None:
    stmt = select(CardTransaction).where(CardTransaction.transaction_id == transaction_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.exec(stmt).first()


def create_card_transaction(session: Session, transaction: CardTransaction) -> CardTransaction:
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    return transaction


def list_pending_refunds(
    session: Session,
    *,
    related_transaction_ids: Sequence[str],
    statuses: Sequence[str],
) -> list[CardTransaction]:
    """退款先于原扣款到达时先挂起，等原交易扣款成功后按它补记。

    同一笔扣款可能被平台拆成多条流水（SETTLED 换新 ID 后用 related 指回原单），
    所以按整条链的 ID 一起找。
    """
    stmt = (
        select(CardTransaction)
        .where(
            CardTransaction.related_transaction_id.in_(related_transaction_ids),
            CardTransaction.event_status.in_(statuses),
            CardTransaction.balance_applied.is_(False),
        )
        .with_for_update()
    )
    return list(session.exec(stmt).all())


def list_related_charges(
    session: Session,
    *,
    related_transaction_id: str,
    statuses: Sequence[str],
) -> list[CardTransaction]:
    """按关联原交易找同一笔扣款的其它流水行，用于跨 transaction_id 去重。"""
    stmt = (
        select(CardTransaction)
        .where(
            CardTransaction.related_transaction_id == related_transaction_id,
            CardTransaction.event_status.in_(statuses),
        )
        .with_for_update()
    )
    return list(session.exec(stmt).all())


def save_card_transaction(
    session: Session,
    transaction: CardTransaction,
    card: Card | None = None,
) -> CardTransaction:
    """流水、卡计数/余额在同一个事务里提交，避免只落了一半。"""
    now = utcnow()
    transaction.updated_at = now
    session.add(transaction)
    if card is not None:
        card.updated_at = now
        session.add(card)
    session.commit()
    session.refresh(transaction)
    return transaction


# ---------- Task（业务状态机）+ TaskOutbox（调度意图） ----------

OUTBOX_RUN = "run"
OUTBOX_CALLBACK = "callback"

# 补卡任务在途判定：这些状态表示同套餐已经有一次开卡没走完
OPEN_CARD_IN_FLIGHT_STATUSES = ("pending", "running", "processing", "waiting_card")


def create_task(
    session: Session,
    *,
    kind: str,
    request_id: str,
    plan: str | None = None,
    payload_ciphertext: str | None = None,
    callback_url: str | None = None,
    api_key_id: int | None = None,
    status: str = "pending",
    run_at: datetime | None = None,
) -> Task:
    """建任务并排一次执行：任务行和 outbox 在同一个事务里落库。

    `run_at` 不传就是马上跑；任务一旦存在就必须有调度意图，否则会永远停在 pending。
    """
    task = Task(
        kind=kind,
        plan=plan,
        request_id=request_id,
        payload_ciphertext=payload_ciphertext,
        callback_url=callback_url,
        api_key_id=api_key_id,
        status=status,
    )
    session.add(task)
    session.flush()
    task.attempts = 1
    session.add(TaskOutbox(task_id=task.id, kind=OUTBOX_RUN, run_at=run_at or utcnow(), token=1))
    session.commit()
    session.refresh(task)
    return task


def get_task(session: Session, task_id: int) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} 不存在")
    return task


def save_task(session: Session, task: Task) -> Task:
    task.updated_at = utcnow()
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def save_task_with_outbox(
    session: Session,
    task: Task,
    *,
    kind: str,
    run_at: datetime,
    token: int | None = None,
) -> Task:
    """任务状态和调度意图一次提交：要么都写进去，要么都不写。"""
    task.updated_at = utcnow()
    session.add(task)
    session.flush()
    session.add(TaskOutbox(task_id=task.id, kind=kind, run_at=run_at, token=token))
    session.commit()
    session.refresh(task)
    return task


def has_pending_outbox(session: Session, task_id: int, kind: str) -> bool:
    """同一种调度是否已经有没被 ack 的行，避免重复排回调。"""
    stmt = (
        select(TaskOutbox.id).where(TaskOutbox.task_id == task_id, TaskOutbox.kind == kind).limit(1)
    )
    return session.exec(stmt).first() is not None


def list_outbox(
    session: Session,
    *,
    limit: int = 20,
    run_kinds: Sequence[str] | None = None,
) -> list[TaskOutbox]:
    """还没被 worker ack 的调度意图，publisher 每轮取一批投递。

    延迟交给 arq 的 `_defer_by`：所以这里不按 run_at 过滤，早投晚跑；
    同一条行用固定 `_job_id` 反复投递，arq 侧去重，Redis 丢了还能补。

    `run_kinds` 只过滤“执行任务”的行（例如 BYOC 没配置时只跑补卡）；
    回调行不受影响，已经产出的结果该发还是要发。
    """
    stmt = (
        select(TaskOutbox)
        .join(Task, Task.id == TaskOutbox.task_id)
        .order_by(col(TaskOutbox.run_at), col(TaskOutbox.id))
        .limit(min(limit, MAX_LIMIT))
    )
    if run_kinds is not None:
        stmt = stmt.where(
            or_(TaskOutbox.kind != OUTBOX_RUN, Task.kind.in_(run_kinds)),
        )
    return list(session.exec(stmt).all())


def delete_outbox(session: Session, outbox_id: int) -> None:
    """worker 开始执行时 ack：删掉这条调度意图，publisher 不再重投。"""
    session.execute(delete(TaskOutbox).where(TaskOutbox.id == outbox_id))
    session.commit()


def task_status_counts(session: Session) -> dict[str, int]:
    """任务队列快照：按状态统计，空队列返回空 dict。"""
    rows = session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    return {status: count for status, count in rows}


def pending_outbox_count(session: Session) -> int:
    """还没被执行/投递确认的调度条数，排查“排队了但没跑”。"""
    return session.execute(select(func.count()).select_from(TaskOutbox)).scalar_one()


def find_open_card_task(session: Session, plan: str) -> Task | None:
    """同一套餐只认一条在途补卡任务；processing 是等 webhook/重试中的补卡。

    waiting_card 只为兼容升级前的旧数据，新代码不再给补卡任务写这个状态。
    """
    stmt = (
        select(Task)
        .where(
            Task.kind == "open_card",
            Task.plan == plan,
            Task.status.in_(OPEN_CARD_IN_FLIGHT_STATUSES),
        )
        .order_by(col(Task.id))
    )
    return session.exec(stmt).first()


# ---------- 订阅用卡锁定 ----------


def find_subscription_candidates(
    session: Session,
    plan: str,
    now: datetime,
    *,
    min_balance: Decimal | None = None,
) -> list[Card]:
    stmt = (
        select(Card)
        .where(
            Card.plan == plan,
            Card.enabled.is_(True),
            Card.used_count < 3,
            Card.card_no.is_not(None),
            Card.expiry.is_not(None),
            Card.cvv.is_not(None),
            or_(Card.reserved_until.is_(None), Card.reserved_until < now),
        )
        .order_by(col(Card.id))
    )
    if min_balance is not None:
        stmt = stmt.where(Card.balance >= min_balance)
    return list(session.exec(stmt).all())


def claim_card(
    session: Session,
    card_id: int,
    task_id: str,
    *,
    until: datetime,
    now: datetime,
) -> bool:
    result = session.execute(
        update(Card)
        .where(
            Card.id == card_id,
            or_(Card.reserved_until.is_(None), Card.reserved_until < now),
        )
        .values(reserved_until=until, reserved_task_id=task_id, updated_at=utcnow())
    )
    session.commit()
    return result.rowcount == 1


def refresh_card_reservation(
    session: Session,
    card_id: int,
    task_id: str,
    *,
    until: datetime,
) -> None:
    session.execute(
        update(Card)
        .where(Card.id == card_id, Card.reserved_task_id == task_id)
        .values(reserved_until=until, updated_at=utcnow())
    )
    session.commit()


def release_card(session: Session, card_id: int, task_id: str) -> None:
    session.execute(
        update(Card)
        .where(Card.id == card_id, Card.reserved_task_id == task_id)
        .values(reserved_until=None, reserved_task_id=None, updated_at=utcnow())
    )
    session.commit()


# ---------- ApiKey ----------


def create_api_key(
    session: Session,
    *,
    name: str,
    key_prefix: str,
    key_hash: str,
    expires_at: datetime | None = None,
) -> ApiKey:
    api_key = ApiKey(
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        expires_at=expires_at,
    )
    session.add(api_key)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(f"API Key 前缀冲突：{key_prefix}") from exc
    session.refresh(api_key)
    return api_key


def get_api_key_by_prefix(session: Session, key_prefix: str) -> ApiKey | None:
    stmt = select(ApiKey).where(ApiKey.key_prefix == key_prefix)
    return session.exec(stmt).first()


def list_api_keys(session: Session) -> list[ApiKey]:
    return list(session.exec(select(ApiKey).order_by(col(ApiKey.id))).all())


def save_api_key(session: Session, api_key: ApiKey) -> ApiKey:
    api_key.updated_at = utcnow()
    session.add(api_key)
    session.commit()
    session.refresh(api_key)
    return api_key


def touch_api_key(session: Session, api_key: ApiKey) -> None:
    api_key.last_used_at = utcnow()
    session.add(api_key)
    session.commit()


# ---------- CardOperation ----------


def apply_card_operation(
    session: Session,
    operation: CardOperation,
    card: Card | None = None,
) -> bool:
    """流水和余额在同一个事务里提交；eventId 冲突说明重复投递，回滚返回 False。"""
    session.add(operation)
    if card is not None:
        card.updated_at = utcnow()
        session.add(card)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    session.refresh(operation)
    return True
