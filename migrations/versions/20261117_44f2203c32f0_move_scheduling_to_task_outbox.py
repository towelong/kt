"""move scheduling to task outbox

调度从 task 表的 next_run_at/claimed_until/callback_next_at 挪到 task_outbox：
业务状态和“什么时候再跑”在同一个事务里写，publisher 投递成 arq 延迟消息。

Revision ID: 44f2203c32f0
Revises: 4b01a6a7cd73
Create Date: 2026-11-17 03:44:43.064344+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "44f2203c32f0"
down_revision: str | None = "4b01a6a7cd73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_outbox",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], name=op.f("fk_task_outbox_task_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_outbox")),
    )
    op.create_index(op.f("ix_task_outbox_run_at"), "task_outbox", ["run_at"], unique=False)
    op.create_index(op.f("ix_task_outbox_task_id"), "task_outbox", ["task_id"], unique=False)
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_callback_next_at"))
        batch_op.drop_index(batch_op.f("ix_task_next_run_at"))
        batch_op.drop_column("next_run_at")
        batch_op.drop_column("callback_next_at")
        batch_op.drop_column("claimed_until")


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        # 老表里已有数据，加 NOT NULL 列必须给个默认值；回滚只求结构可用
        batch_op.add_column(
            sa.Column(
                "next_run_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("'1970-01-01 00:00:00'"),
            )
        )
        batch_op.add_column(
            sa.Column("callback_next_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_task_next_run_at"), ["next_run_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_task_callback_next_at"), ["callback_next_at"], unique=False
        )
    op.drop_index(op.f("ix_task_outbox_task_id"), table_name="task_outbox")
    op.drop_index(op.f("ix_task_outbox_run_at"), table_name="task_outbox")
    op.drop_table("task_outbox")
