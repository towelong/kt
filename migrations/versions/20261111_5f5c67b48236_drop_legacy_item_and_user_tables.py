"""drop legacy item and user tables

骨架演示用的两张表已经和业务无关，删除。downgrade 会按原始结构恢复，便于回退。

Revision ID: 5f5c67b48236
Revises: 414da2b81a08
Create Date: 2026-11-11 07:52:00.112820+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f5c67b48236"
down_revision: str | None = "414da2b81a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_email"))
        batch_op.drop_index(batch_op.f("ix_user_username"))
    op.drop_table("user")

    with op.batch_alter_table("item", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_item_name"))
    op.drop_table("item")


def downgrade() -> None:
    op.create_table(
        "item",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item")),
    )
    with op.batch_alter_table("item", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_item_name"), ["name"], unique=False)

    op.create_table(
        "user",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user")),
    )
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_email"), ["email"], unique=True)
        batch_op.create_index(batch_op.f("ix_user_username"), ["username"], unique=True)
