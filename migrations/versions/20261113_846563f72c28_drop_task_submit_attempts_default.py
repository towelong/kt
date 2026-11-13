"""drop task submit attempts default

Revision ID: 846563f72c28
Revises: a1c7f3b92d10
Create Date: 2026-11-13 09:56:46.667210+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "846563f72c28"
down_revision: str | None = "a1c7f3b92d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 上一条迁移加列时用了 server_default=0，MySQL 上的 DROP DEFAULT 没生效；
    # 这里显式再改一次，让库结构和模型的 Python 侧默认值保持一致。
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.alter_column(
            "submit_attempts",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.alter_column(
            "submit_attempts",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=sa.text("0"),
        )
