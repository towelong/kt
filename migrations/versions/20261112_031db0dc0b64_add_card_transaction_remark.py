"""add card transaction remark

Revision ID: 031db0dc0b64
Revises: 35e895188595
Create Date: 2026-11-12 04:01:28.646623+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "031db0dc0b64"
down_revision: str | None = "35e895188595"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("card_transaction", schema=None) as batch_op:
        batch_op.add_column(sa.Column("remark", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("card_transaction", schema=None) as batch_op:
        batch_op.drop_column("remark")
