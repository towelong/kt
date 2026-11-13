"""add task submit attempts

BYOC 取报价/校验/建单失败改为保留 payload 重试，用 submit_attempts 计数。

Revision ID: a1c7f3b92d10
Revises: d74b310024d9
Create Date: 2026-11-13 10:20:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c7f3b92d10"
down_revision: str | None = "d74b310024d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        # 先带默认值加列，给历史行补 0；再把默认值去掉，和模型保持一致
        batch_op.add_column(
            sa.Column(
                "submit_attempts",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.alter_column(
            "submit_attempts",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("submit_attempts")
