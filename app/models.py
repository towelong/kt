"""领域模型与请求/响应模型。所有字段长度都显式给出，MySQL 才能建表。"""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel

# 约束/索引名必须确定，否则 SQLite 上改表（batch 模式）和 MySQL 上的 drop 都会猜名字。
# 名字也控制在 MySQL 的 64 字符标识符上限内。
SQLModel.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """带时区的 UTC；SQLModel 的 UTCDateTime 负责在 MySQL 上落成无偏移的 UTC 值。"""
    return datetime.now(UTC)


class TimestampMixin(SQLModel):
    created_at: datetime = Field(default_factory=utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class UserBase(SQLModel):
    username: str = Field(index=True, unique=True, max_length=64)
    email: str = Field(index=True, unique=True, max_length=255)
    is_active: bool = Field(default=True)


class User(TimestampMixin, UserBase, table=True):
    __tablename__ = "user"

    id: int | None = Field(default=None, primary_key=True)


class UserCreate(UserBase):
    pass


class UserPublic(UserBase):
    id: int
    created_at: datetime


class ItemBase(SQLModel):
    name: str = Field(index=True, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    price: float = Field(default=0, ge=0)


class Item(TimestampMixin, ItemBase, table=True):
    __tablename__ = "item"

    id: int | None = Field(default=None, primary_key=True)


class ItemCreate(ItemBase):
    pass


class ItemUpdate(SQLModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    price: float | None = Field(default=None, ge=0)


class ItemPublic(ItemBase):
    id: int
    created_at: datetime
    updated_at: datetime
