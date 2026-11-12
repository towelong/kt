"""quote id to text

BYOC 的 quote_id 长度不稳定，MySQL VARCHAR(128) 会报 1406，改成 TEXT。

Revision ID: ef59d5d449fd
Revises: 5d3840946d15
Create Date: 2026-11-12 02:31:00.480582+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ef59d5d449fd"
down_revision: str | None = "5d3840946d15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch 模式：SQLite 靠重建表，MySQL 直接 MODIFY
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.alter_column(
            "quote_id",
            existing_type=sa.String(length=128),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.alter_column(
            "quote_id",
            existing_type=sa.Text(),
            type_=sa.String(length=128),
            existing_nullable=True,
        )
