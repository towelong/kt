"""add card transaction currencies

Revision ID: 35e895188595
Revises: ef59d5d449fd
Create Date: 2026-11-12 03:56:37.965153+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "35e895188595"
down_revision: str | None = "ef59d5d449fd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch 模式：SQLite 也能安全加列/回退
    with op.batch_alter_table("card_transaction", schema=None) as batch_op:
        batch_op.add_column(sa.Column("original_currency", sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column("actual_currency", sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("card_transaction", schema=None) as batch_op:
        batch_op.drop_column("actual_currency")
        batch_op.drop_column("original_currency")
