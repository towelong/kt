"""drop job lease table

任务调度改由 arq（Redis）承担，多副本定时任务用 cron 的 unique 保证只跑一次，
数据库里的 job_lease 租约表不再需要。

Revision ID: 4b01a6a7cd73
Revises: a93cb4061281
Create Date: 2026-11-17 03:28:41.949633+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b01a6a7cd73"
down_revision: str | None = "a93cb4061281"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("job_lease")


def downgrade() -> None:
    op.create_table(
        "job_lease",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_job_lease")),
    )
