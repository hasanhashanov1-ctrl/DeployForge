import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import DeploymentStatus, LifecycleState, ProjectState


def utc_now() -> datetime:
    return datetime.now(UTC)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    repo_url: Mapped[str] = mapped_column(String(500))
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dockerfile_path: Mapped[str] = mapped_column(String(500), default="Dockerfile")
    container_port: Mapped[int] = mapped_column(Integer)
    state: Mapped[ProjectState] = mapped_column(
        Enum(ProjectState, native_enum=False, length=20), default=ProjectState.ACTIVE
    )
    lifecycle_state: Mapped[LifecycleState] = mapped_column(
        Enum(LifecycleState, native_enum=False, length=20),
        default=LifecycleState.ACTIVE,
    )
    operation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleanup_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    deployments: Mapped[list["Deployment"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    variables: Mapped[list["ProjectVariable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectVariable(Base):
    __tablename__ = "project_variables"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_project_variable_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(128))
    encrypted_value: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    project: Mapped[Project] = relationship(back_populates="variables")


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("project_id", "active_token", name="uq_deployment_project_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus, native_enum=False, length=20),
        default=DeploymentStatus.QUEUED,
        index=True,
    )
    active_token: Mapped[str | None] = mapped_column(String(10), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    image_tag: Mapped[str | None] = mapped_column(String(300), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    build_log: Mapped[str] = mapped_column(Text, default="")
    runtime_log: Mapped[str] = mapped_column(Text, default="")
    build_log_truncated: Mapped[bool] = mapped_column(default=False)
    runtime_log_truncated: Mapped[bool] = mapped_column(default=False)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="deployments")
