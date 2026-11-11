"""数据访问层：只碰 Session 和模型，不碰 HTTP。分页统一 offset/limit。"""

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from app.models import (
    Item,
    ItemCreate,
    ItemUpdate,
    User,
    UserCreate,
    utcnow,
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class ConflictError(Exception):
    """唯一约束冲突，由路由层翻译成 409，不依赖数据库错误码。"""


class NotFoundError(Exception):
    """资源不存在，由路由层翻译成 404。"""


# ---------- Item ----------


def get_item(session: Session, item_id: int) -> Item:
    item = session.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"item {item_id} 不存在")
    return item


def list_items(
    session: Session, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> tuple[int, list[Item]]:
    limit = max(1, min(limit, MAX_LIMIT))
    total = session.exec(select(func.count()).select_from(Item)).one()
    rows = session.exec(select(Item).order_by(col(Item.id)).offset(offset).limit(limit)).all()
    return int(total), list(rows)


def create_item(session: Session, payload: ItemCreate) -> Item:
    item = Item.model_validate(payload)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def update_item(session: Session, item_id: int, payload: ItemUpdate) -> Item:
    item = get_item(session, item_id)
    data = payload.model_dump(exclude_unset=True)
    if data:
        item.sqlmodel_update(data)
        item.updated_at = utcnow()
        session.add(item)
        session.commit()
        session.refresh(item)
    return item


def delete_item(session: Session, item_id: int) -> None:
    item = get_item(session, item_id)
    session.delete(item)
    session.commit()


# ---------- User ----------


def get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"user {user_id} 不存在")
    return user


def get_user_by_username(session: Session, username: str) -> User | None:
    return session.exec(select(User).where(User.username == username)).first()


def list_users(
    session: Session, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> tuple[int, list[User]]:
    limit = max(1, min(limit, MAX_LIMIT))
    total = session.exec(select(func.count()).select_from(User)).one()
    rows = session.exec(select(User).order_by(col(User.id)).offset(offset).limit(limit)).all()
    return int(total), list(rows)


def create_user(session: Session, payload: UserCreate) -> User:
    user = User.model_validate(payload)
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:  # 唯一索引冲突，两个数据库都抛这个
        session.rollback()
        raise ConflictError(f"用户名或邮箱已存在：{payload.username}") from exc
    session.refresh(user)
    return user
