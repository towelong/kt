"""add card transaction applied amount

Revision ID: a93cb4061281
Revises: 846563f72c28
Create Date: 2026-11-16 03:46:51.321133+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a93cb4061281"
down_revision: str | None = "846563f72c28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("card_transaction", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("applied_amount", sa.Numeric(precision=18, scale=2), nullable=True)
        )

    # 旧口径已经扣过款的流水，用当前 actual_amount 回填；
    # 早期阶段推进导致的金额差异无法还原，这里只保证失败退回时有值可用。
    op.execute(
        "UPDATE card_transaction SET applied_amount = actual_amount WHERE balance_applied = 1"
    )
    op.execute("UPDATE card_transaction SET applied_amount = 0 WHERE applied_amount IS NULL")

    with op.batch_alter_table("card_transaction", schema=None) as batch_op:
        batch_op.alter_column(
            "applied_amount",
            existing_type=sa.Numeric(precision=18, scale=2),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("card_transaction", schema=None) as batch_op:
        batch_op.drop_column("applied_amount")
