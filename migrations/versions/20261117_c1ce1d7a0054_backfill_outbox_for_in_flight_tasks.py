"""backfill outbox for in flight tasks

上一条迁移把调度列删了，升级时还没到终态的任务需要补一条 outbox，
否则发布后会一直停在 pending。新库上没有这类数据，等于空操作。

run_at 用一个过去的时间点，表示“尽快跑”；token 取自增后的 attempts，
和 worker 的消息令牌校验保持一致（旧令牌的消息会被丢弃）。

Revision ID: c1ce1d7a0054
Revises: 44f2203c32f0
Create Date: 2026-11-17 03:47:57.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1ce1d7a0054"
down_revision: str | None = "44f2203c32f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENDING = "status NOT IN ('succeeded', 'failed')"


def upgrade() -> None:
    op.execute(f"UPDATE task SET attempts = attempts + 1 WHERE {PENDING}")
    op.execute(
        "INSERT INTO task_outbox (created_at, updated_at, task_id, kind, run_at, token) "
        f"SELECT CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, id, 'run', '1970-01-01 00:00:00', attempts "
        f"FROM task WHERE {PENDING}"
    )


def downgrade() -> None:
    # 数据修补无法和正常调度区分开，回滚时保留：最多让任务多跑一次，由状态机兜底
    pass
