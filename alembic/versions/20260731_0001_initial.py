"""Initial DeployForge schema."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260731_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("repo_url", sa.String(length=500), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("dockerfile_path", sa.String(length=500), nullable=False),
        sa.Column("container_port", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum("ACTIVE", "DELETING", name="projectstate", native_enum=False, length=20),
            nullable=False,
        ),
        sa.Column("cleanup_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_projects_slug"), "projects", ["slug"], unique=True)
    op.create_table(
        "deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "CLONING",
                "BUILDING",
                "STARTING",
                "RUNNING",
                "FAILED",
                "STOPPED",
                name="deploymentstatus",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("active_token", sa.String(length=10), nullable=True),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("image_tag", sa.String(length=300), nullable=True),
        sa.Column("container_id", sa.String(length=100), nullable=True),
        sa.Column("container_name", sa.String(length=200), nullable=True),
        sa.Column("build_log", sa.Text(), nullable=False),
        sa.Column("runtime_log", sa.Text(), nullable=False),
        sa.Column("build_log_truncated", sa.Boolean(), nullable=False),
        sa.Column("runtime_log_truncated", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "active_token", name="uq_deployment_project_active"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index(op.f("ix_deployments_project_id"), "deployments", ["project_id"], unique=False)
    op.create_index(op.f("ix_deployments_status"), "deployments", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_deployments_status"), table_name="deployments")
    op.drop_index(op.f("ix_deployments_project_id"), table_name="deployments")
    op.drop_table("deployments")
    op.drop_index(op.f("ix_projects_slug"), table_name="projects")
    op.drop_table("projects")
