"""Add cooperative deployment cancellation."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260801_0004"
down_revision: str | None = "20260801_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.alter_column("deployments", "cancel_requested", server_default=None)


def downgrade() -> None:
    op.drop_column("deployments", "cancel_requested")
