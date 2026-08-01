"""Add project lifecycle controls."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260801_0003"
down_revision: str | None = "20260801_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    lifecycle = sa.Enum(
        "ACTIVE",
        "STOPPING",
        "STOPPED",
        "STARTING",
        "ROLLING_BACK",
        name="lifecyclestate",
        native_enum=False,
        length=20,
    )
    op.add_column(
        "projects",
        sa.Column("lifecycle_state", lifecycle, server_default="ACTIVE", nullable=False),
    )
    op.add_column("projects", sa.Column("operation_error", sa.Text(), nullable=True))
    op.alter_column("projects", "lifecycle_state", server_default=None)


def downgrade() -> None:
    op.drop_column("projects", "operation_error")
    op.drop_column("projects", "lifecycle_state")
