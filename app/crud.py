"""数据访问层：只碰 Session 和模型，不碰 HTTP。分页统一 offset/limit。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.models import (
    ApiKey,
    Card,
    CardOperation,
    CardTransaction,
    JobLease,
    Task,
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
    recharge_amount: Decimal,
) -> Card:
    """先落占位行再调申请卡：超时后能靠 request_no 认出“已提交过”，不重复开卡。"""
    card = Card(
        plan=plan,
        request_no=request_no,
        balance=recharge_amount,
        apply_status="PENDING",
        enabled=False,
    )
    session.add(card)
    session.commit()
    session.refresh(card)
    return card


def get_card_by_request_no(session: Session, request_no: str) -> Card | None:
    return session.exec(select(Card).where(Card.request_no == request_no)).first()


def get_card_by_platform_id(session: Session, card_id: str) -> Card | None:
    return session.exec(select(Card).where(Card.card_id == card_id)).first()


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
    """近期提交、但还没变成可用卡（等 webhook 补三要素）的占位，避免重复开卡。

    次数用完被停用的卡不算“未完成”，它应该被下一轮补掉。
    """
    stmt = (
        select(Card)
        .where(
            Card.plan == plan,
            Card.enabled.is_(False),
            Card.created_at >= since,
            or_(Card.apply_status == "PENDING", Card.card_no.is_(None)),
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


def get_card_transaction(session: Session, transaction_id: str) -> CardTransaction | None:
    stmt = select(CardTransaction).where(CardTransaction.transaction_id == transaction_id)
    return session.exec(stmt).first()


def create_card_transaction(session: Session, transaction: CardTransaction) -> CardTransaction:
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    return transaction


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


# ---------- JobLease ----------


def ensure_job_lease(session: Session, name: str) -> None:
    if session.get(JobLease, name) is not None:
        return
    session.add(JobLease(name=name))
    try:
        session.commit()
    except IntegrityError:
        # 另一个副本同时插入，谁先提交都一样，忽略即可
        session.rollback()


def acquire_job_lease(session: Session, name: str, *, locked_until: datetime) -> bool:
    """条件 UPDATE 抢锁，SQLite 和 MySQL 语义一致；rowcount=1 表示抢到。"""
    ensure_job_lease(session, name)
    stmt = (
        update(JobLease)
        .where(JobLease.name == name, JobLease.locked_until <= utcnow())
        .values(locked_until=locked_until, updated_at=utcnow())
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


def release_job_lease(session: Session, name: str) -> None:
    stmt = (
        update(JobLease)
        .where(JobLease.name == name)
        .values(locked_until=utcnow(), updated_at=utcnow())
    )
    session.execute(stmt)
    session.commit()


# ---------- Task（数据库任务队列） ----------

CLAIMABLE_TASK_STATUSES = (
    "pending",
    "waiting_card",
    "waiting_quote",
    "processing",
    "waiting_action",
    "unknown",
    # 认领后 worker 崩溃的任务；只有 claimed_until 过期才会被重新抢到
    "running",
)


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
    next_run_at: datetime | None = None,
) -> Task:
    task = Task(
        kind=kind,
        plan=plan,
        request_id=request_id,
        payload_ciphertext=payload_ciphertext,
        callback_url=callback_url,
        api_key_id=api_key_id,
        status=status,
        next_run_at=next_run_at or utcnow(),
    )
    session.add(task)
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


def claim_next_task(session: Session, *, now: datetime, claim_until: datetime) -> Task | None:
    """条件 UPDATE 抢一条待执行任务；多副本只有一个能抢到。"""
    free = or_(Task.claimed_until.is_(None), Task.claimed_until < now)
    candidate = session.exec(
        select(Task.id)
        .where(Task.status.in_(CLAIMABLE_TASK_STATUSES), Task.next_run_at <= now, free)
        .order_by(col(Task.next_run_at), col(Task.id))
        .limit(1)
    ).first()
    if candidate is None:
        return None
    result = session.execute(
        update(Task)
        .where(
            Task.id == candidate,
            Task.status.in_(CLAIMABLE_TASK_STATUSES),
            Task.next_run_at <= now,
            free,
        )
        .values(status="running", claimed_until=claim_until, attempts=Task.attempts + 1)
    )
    session.commit()
    if result.rowcount != 1:
        return None
    return session.get(Task, candidate)


def find_open_card_task(session: Session, plan: str) -> Task | None:
    stmt = (
        select(Task)
        .where(
            Task.kind == "open_card",
            Task.plan == plan,
            Task.status.in_(("pending", "running", "waiting_card")),
        )
        .order_by(col(Task.id))
    )
    return session.exec(stmt).first()


def list_due_callbacks(session: Session, now: datetime, limit: int = 20) -> list[Task]:
    stmt = (
        select(Task)
        .where(
            Task.callback_url.is_not(None),
            Task.callback_delivered.is_(False),
            Task.callback_next_at.is_not(None),
            Task.callback_next_at <= now,
        )
        .order_by(col(Task.callback_next_at), col(Task.id))
        .limit(limit)
    )
    return list(session.exec(stmt).all())


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
