import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enums import DeploymentStatus, LifecycleState, ProjectState
from app.validation import (
    normalize_github_url,
    validate_branch,
    validate_dockerfile_path,
    validate_environment_key,
    validate_environment_value,
    validate_slug,
)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    slug: str
    repo_url: str
    branch: str | None = None
    dockerfile_path: str = "Dockerfile"
    container_port: int = Field(ge=1, le=65535)

    _validate_slug = field_validator("slug")(validate_slug)
    _normalize_repo_url = field_validator("repo_url")(normalize_github_url)
    _validate_branch = field_validator("branch")(validate_branch)
    _validate_dockerfile_path = field_validator("dockerfile_path")(validate_dockerfile_path)


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    dockerfile_path: str | None = None
    container_port: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("slug")
    @classmethod
    def validate_optional_slug(cls, value: str | None) -> str | None:
        return validate_slug(value) if value is not None else None

    @field_validator("repo_url")
    @classmethod
    def normalize_optional_repo_url(cls, value: str | None) -> str | None:
        return normalize_github_url(value) if value is not None else None

    _validate_branch = field_validator("branch")(validate_branch)

    @field_validator("dockerfile_path")
    @classmethod
    def validate_optional_dockerfile_path(cls, value: str | None) -> str | None:
        return validate_dockerfile_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_patch(self) -> "ProjectUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one project field must be provided")
        nullable_fields = {"branch"}
        for field in self.model_fields_set - nullable_fields:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    repo_url: str
    branch: str | None
    dockerfile_path: str
    container_port: int
    state: ProjectState
    lifecycle_state: LifecycleState
    operation_error: str | None
    cleanup_error: str | None
    latest_status: DeploymentStatus | None
    public_url: str
    created_at: datetime
    updated_at: datetime


class DeploymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    task_id: str
    status: DeploymentStatus
    commit_sha: str | None
    image_tag: str | None
    container_id: str | None
    container_name: str | None
    cancel_requested: bool
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class DeploymentLogsResponse(BaseModel):
    deployment_id: uuid.UUID
    status: DeploymentStatus
    build_log: str
    runtime_log: str
    build_log_truncated: bool
    runtime_log_truncated: bool


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str


class ProjectVariableInput(BaseModel):
    key: str
    value: str | None = None
    is_secret: bool = False

    _validate_key = field_validator("key")(validate_environment_key)
    _validate_value = field_validator("value")(validate_environment_value)


class ProjectVariablesUpdate(BaseModel):
    variables: list[ProjectVariableInput] = Field(max_length=100)

    @model_validator(mode="after")
    def keys_must_be_unique(self) -> "ProjectVariablesUpdate":
        keys = [item.key for item in self.variables]
        if len(keys) != len(set(keys)):
            raise ValueError("environment variable keys must be unique")
        return self


class ProjectVariableResponse(BaseModel):
    key: str
    value: str | None
    is_secret: bool
    has_value: bool = True


class RollbackRequest(BaseModel):
    deployment_id: uuid.UUID
