import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import desc, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.enums import (
    IN_PROGRESS_STATUSES,
    LIFECYCLE_IN_PROGRESS_STATES,
    DeploymentStatus,
    LifecycleState,
    ProjectState,
)
from app.models import Deployment, Project, ProjectVariable
from app.queue import (
    enqueue_deployment,
    enqueue_project_cleanup,
    enqueue_project_rollback,
    enqueue_project_start,
    enqueue_project_stop,
)
from app.schemas import (
    DeploymentLogsResponse,
    DeploymentResponse,
    HealthResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    ProjectVariableResponse,
    ProjectVariablesUpdate,
    RollbackRequest,
)
from app.services.secrets import SecretCipher, SecretCipherError

router = APIRouter()
SessionDep = Annotated[AsyncSession, Depends(get_session)]
MAX_ENVIRONMENT_BYTES = 131_072


def public_url(slug: str) -> str:
    port = get_settings().http_port
    suffix = "" if port == 80 else f":{port}"
    return f"http://{slug}.localhost{suffix}"


async def latest_deployment(session: AsyncSession, project_id: uuid.UUID) -> Deployment | None:
    result = await session.execute(
        select(Deployment)
        .where(Deployment.project_id == project_id)
        .order_by(desc(Deployment.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def project_response(session: AsyncSession, project: Project) -> ProjectResponse:
    running_result = await session.execute(
        select(Deployment)
        .where(
            Deployment.project_id == project.id,
            Deployment.status == DeploymentStatus.RUNNING,
        )
        .order_by(desc(Deployment.created_at))
        .limit(1)
    )
    latest = running_result.scalar_one_or_none() or await latest_deployment(session, project.id)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        slug=project.slug,
        repo_url=project.repo_url,
        branch=project.branch,
        dockerfile_path=project.dockerfile_path,
        container_port=project.container_port,
        state=project.state,
        lifecycle_state=project.lifecycle_state,
        operation_error=project.operation_error,
        cleanup_error=project.cleanup_error,
        latest_status=latest.status if latest else None,
        public_url=public_url(project.slug),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(session: SessionDep) -> HealthResponse:
    try:
        await session.execute(text("SELECT 1"))
        redis = Redis.from_url(get_settings().redis_url)
        try:
            await redis.ping()
        finally:
            await redis.aclose()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="A required service is unavailable") from exc
    return HealthResponse(status="ok", database="ok", redis="ok")


@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
)
async def create_project(payload: ProjectCreate, session: SessionDep) -> ProjectResponse:
    project = Project(**payload.model_dump())
    session.add(project)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="A project with this slug already exists"
        ) from exc
    await session.refresh(project)
    return await project_response(session, project)


@router.get("/projects", response_model=list[ProjectResponse], tags=["projects"])
async def list_projects(session: SessionDep) -> list[ProjectResponse]:
    result = await session.execute(select(Project).order_by(desc(Project.created_at)))
    return [await project_response(session, item) for item in result.scalars()]


async def get_project_or_404(session: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}", response_model=ProjectResponse, tags=["projects"])
async def get_project(project_id: uuid.UUID, session: SessionDep) -> ProjectResponse:
    project = await get_project_or_404(session, project_id)
    return await project_response(session, project)


def ensure_lifecycle_idle(project: Project) -> None:
    if project.lifecycle_state in LIFECYCLE_IN_PROGRESS_STATES:
        raise HTTPException(status_code=409, detail="A project operation is already in progress")


async def ensure_no_active_deployment(session: AsyncSession, project_id: uuid.UUID) -> None:
    active = await session.execute(
        select(Deployment.id).where(
            Deployment.project_id == project_id,
            Deployment.status.in_(IN_PROGRESS_STATUSES),
        )
    )
    if active.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="A deployment is already in progress")


@router.patch("/projects/{project_id}", response_model=ProjectResponse, tags=["projects"])
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: SessionDep,
) -> ProjectResponse:
    project = await get_project_or_404(session, project_id)
    if project.state is ProjectState.DELETING:
        raise HTTPException(status_code=409, detail="Project is being deleted")
    ensure_lifecycle_idle(project)
    await ensure_no_active_deployment(session, project_id)

    changes = payload.model_dump(exclude_unset=True)
    immutable_changes = {
        field
        for field in {"slug", "container_port"}
        if field in changes and changes[field] != getattr(project, field)
    }
    if immutable_changes:
        container = await session.execute(
            select(Deployment.id).where(
                Deployment.project_id == project_id,
                Deployment.container_id.is_not(None),
            )
        )
        if container.first() is not None:
            fields = ", ".join(sorted(immutable_changes))
            raise HTTPException(
                status_code=409,
                detail=f"Cannot change {fields} after a container has been created",
            )

    for field, value in changes.items():
        setattr(project, field, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="A project with this slug already exists"
        ) from exc
    await session.refresh(project)
    return await project_response(session, project)


async def project_variable_responses(
    session: AsyncSession, project_id: uuid.UUID, cipher: SecretCipher
) -> list[ProjectVariableResponse]:
    result = await session.execute(
        select(ProjectVariable)
        .where(ProjectVariable.project_id == project_id)
        .order_by(ProjectVariable.key)
    )
    responses: list[ProjectVariableResponse] = []
    try:
        for variable in result.scalars():
            responses.append(
                ProjectVariableResponse(
                    key=variable.key,
                    value=None if variable.is_secret else cipher.decrypt(variable.encrypted_value),
                    is_secret=variable.is_secret,
                    has_value=True,
                )
            )
    except SecretCipherError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return responses


@router.get(
    "/projects/{project_id}/variables",
    response_model=list[ProjectVariableResponse],
    tags=["projects"],
)
async def list_project_variables(
    project_id: uuid.UUID, session: SessionDep
) -> list[ProjectVariableResponse]:
    await get_project_or_404(session, project_id)
    cipher = SecretCipher(get_settings().secret_key)
    return await project_variable_responses(session, project_id, cipher)


@router.put(
    "/projects/{project_id}/variables",
    response_model=list[ProjectVariableResponse],
    tags=["projects"],
)
async def replace_project_variables(
    project_id: uuid.UUID,
    payload: ProjectVariablesUpdate,
    session: SessionDep,
) -> list[ProjectVariableResponse]:
    project = await get_project_or_404(session, project_id)
    if project.state is ProjectState.DELETING:
        raise HTTPException(status_code=409, detail="Project is being deleted")
    ensure_lifecycle_idle(project)

    try:
        await ensure_no_active_deployment(session, project_id)
    except HTTPException as exc:
        raise HTTPException(
            status_code=409,
            detail="Wait for the active deployment before changing environment variables",
        ) from exc

    cipher = SecretCipher(get_settings().secret_key)
    stored_result = await session.execute(
        select(ProjectVariable).where(ProjectVariable.project_id == project_id)
    )
    stored = {item.key: item for item in stored_result.scalars()}
    values: dict[str, str] = {}

    try:
        for item in payload.variables:
            existing = stored.get(item.key)
            if item.value is None:
                if existing is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"A value is required for new variable {item.key}",
                    )
                values[item.key] = cipher.decrypt(existing.encrypted_value)
            else:
                values[item.key] = item.value
    except SecretCipherError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    total_bytes = sum(
        len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in values.items()
    )
    if total_bytes > MAX_ENVIRONMENT_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Project environment must be at most {MAX_ENVIRONMENT_BYTES} bytes",
        )

    desired_keys = set(values)
    for key, existing in stored.items():
        if key not in desired_keys:
            await session.delete(existing)

    items_by_key = {item.key: item for item in payload.variables}
    for key, value in values.items():
        payload_item = items_by_key[key]
        variable = stored.get(key)
        if variable is None:
            variable = ProjectVariable(project_id=project_id, key=key)
            session.add(variable)
        if payload_item.value is not None:
            variable.encrypted_value = cipher.encrypt(value)
        variable.is_secret = payload_item.is_secret

    await session.commit()
    return await project_variable_responses(session, project_id, cipher)


@router.post(
    "/projects/{project_id}/deploy",
    response_model=DeploymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["deployments"],
)
async def create_deployment(project_id: uuid.UUID, session: SessionDep) -> Deployment:
    project = await get_project_or_404(session, project_id)
    if project.state is ProjectState.DELETING:
        raise HTTPException(status_code=409, detail="Project is being deleted")
    ensure_lifecycle_idle(project)
    await ensure_no_active_deployment(session, project_id)

    deployment_id = uuid.uuid4()
    task_id = f"deploy-{deployment_id}"
    deployment = Deployment(
        id=deployment_id,
        project_id=project_id,
        task_id=task_id,
        status=DeploymentStatus.QUEUED,
        active_token="active",
    )
    session.add(deployment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A deployment is already in progress") from exc

    try:
        enqueue_deployment(deployment.id, task_id)
    except Exception as exc:
        deployment.status = DeploymentStatus.FAILED
        deployment.active_token = None
        deployment.error_message = "Could not enqueue the deployment task"
        deployment.finished_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(status_code=503, detail=deployment.error_message) from exc
    await session.refresh(deployment)
    return deployment


@router.post(
    "/projects/{project_id}/stop",
    response_model=ProjectResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["projects"],
)
async def stop_project(project_id: uuid.UUID, session: SessionDep) -> ProjectResponse:
    project = await get_project_or_404(session, project_id)
    if project.state is ProjectState.DELETING:
        raise HTTPException(status_code=409, detail="Project is being deleted")
    ensure_lifecycle_idle(project)
    if project.lifecycle_state is LifecycleState.STOPPED:
        raise HTTPException(status_code=409, detail="Project is already stopped")
    await ensure_no_active_deployment(session, project_id)
    running = await session.execute(
        select(Deployment.id).where(
            Deployment.project_id == project_id,
            Deployment.status == DeploymentStatus.RUNNING,
        )
    )
    if running.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Project has no running deployment")

    project.lifecycle_state = LifecycleState.STOPPING
    project.operation_error = None
    await session.commit()
    try:
        enqueue_project_stop(project.id)
    except Exception as exc:
        project.lifecycle_state = LifecycleState.ACTIVE
        project.operation_error = "Could not enqueue the stop task"
        await session.commit()
        raise HTTPException(status_code=503, detail=project.operation_error) from exc
    return await project_response(session, project)


@router.post(
    "/projects/{project_id}/start",
    response_model=ProjectResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["projects"],
)
async def start_project(project_id: uuid.UUID, session: SessionDep) -> ProjectResponse:
    project = await get_project_or_404(session, project_id)
    if project.state is ProjectState.DELETING:
        raise HTTPException(status_code=409, detail="Project is being deleted")
    ensure_lifecycle_idle(project)
    if project.lifecycle_state is not LifecycleState.STOPPED:
        raise HTTPException(status_code=409, detail="Project is not stopped")
    await ensure_no_active_deployment(session, project_id)
    stopped = await session.execute(
        select(Deployment.id).where(
            Deployment.project_id == project_id,
            Deployment.status == DeploymentStatus.STOPPED,
            Deployment.container_id.is_not(None),
            Deployment.image_tag.is_not(None),
        )
    )
    if stopped.first() is None:
        raise HTTPException(status_code=409, detail="Project has no stopped deployment")

    project.lifecycle_state = LifecycleState.STARTING
    project.operation_error = None
    await session.commit()
    try:
        enqueue_project_start(project.id)
    except Exception as exc:
        project.lifecycle_state = LifecycleState.STOPPED
        project.operation_error = "Could not enqueue the start task"
        await session.commit()
        raise HTTPException(status_code=503, detail=project.operation_error) from exc
    return await project_response(session, project)


@router.post(
    "/projects/{project_id}/rollback",
    response_model=ProjectResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["projects"],
)
async def rollback_project(
    project_id: uuid.UUID,
    payload: RollbackRequest,
    session: SessionDep,
) -> ProjectResponse:
    project = await get_project_or_404(session, project_id)
    if project.state is ProjectState.DELETING:
        raise HTTPException(status_code=409, detail="Project is being deleted")
    ensure_lifecycle_idle(project)
    if project.lifecycle_state is not LifecycleState.ACTIVE:
        raise HTTPException(status_code=409, detail="Project must be running for rollback")
    await ensure_no_active_deployment(session, project_id)

    current = await session.execute(
        select(Deployment.id).where(
            Deployment.project_id == project_id,
            Deployment.status == DeploymentStatus.RUNNING,
        )
    )
    if current.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Project has no running deployment")
    target = await session.get(Deployment, payload.deployment_id)
    if (
        target is None
        or target.project_id != project_id
        or target.status is not DeploymentStatus.STOPPED
        or target.container_id is None
        or target.image_tag is None
    ):
        raise HTTPException(status_code=409, detail="Rollback target is not available")

    project.lifecycle_state = LifecycleState.ROLLING_BACK
    project.operation_error = None
    await session.commit()
    try:
        enqueue_project_rollback(project.id, target.id)
    except Exception as exc:
        project.lifecycle_state = LifecycleState.ACTIVE
        project.operation_error = "Could not enqueue the rollback task"
        await session.commit()
        raise HTTPException(status_code=503, detail=project.operation_error) from exc
    return await project_response(session, project)


@router.get(
    "/projects/{project_id}/deployments",
    response_model=list[DeploymentResponse],
    tags=["deployments"],
)
async def list_deployments(project_id: uuid.UUID, session: SessionDep) -> list[Deployment]:
    await get_project_or_404(session, project_id)
    result = await session.execute(
        select(Deployment)
        .where(Deployment.project_id == project_id)
        .order_by(desc(Deployment.created_at))
    )
    return list(result.scalars())


async def get_deployment_or_404(session: AsyncSession, deployment_id: uuid.UUID) -> Deployment:
    deployment = await session.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return deployment


@router.get(
    "/deployments/{deployment_id}",
    response_model=DeploymentResponse,
    tags=["deployments"],
)
async def get_deployment(deployment_id: uuid.UUID, session: SessionDep) -> Deployment:
    return await get_deployment_or_404(session, deployment_id)


@router.post(
    "/deployments/{deployment_id}/cancel",
    response_model=DeploymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["deployments"],
)
async def cancel_deployment(deployment_id: uuid.UUID, session: SessionDep) -> Deployment:
    deployment = await get_deployment_or_404(session, deployment_id)
    if deployment.status is DeploymentStatus.CANCELLED:
        return deployment
    if deployment.status not in IN_PROGRESS_STATUSES:
        raise HTTPException(status_code=409, detail="Deployment cannot be cancelled")

    deployment.cancel_requested = True
    if deployment.status is DeploymentStatus.QUEUED:
        deployment.status = DeploymentStatus.CANCELLED
        deployment.active_token = None
        deployment.finished_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(deployment)
    return deployment


def tail_lines(value: str, tail: int) -> str:
    return "\n".join(value.splitlines()[-tail:])


def deployment_logs_response(deployment: Deployment, tail: int) -> DeploymentLogsResponse:
    return DeploymentLogsResponse(
        deployment_id=deployment.id,
        status=deployment.status,
        build_log=tail_lines(deployment.build_log, tail),
        runtime_log=tail_lines(deployment.runtime_log, tail),
        build_log_truncated=deployment.build_log_truncated,
        runtime_log_truncated=deployment.runtime_log_truncated,
    )


@router.get(
    "/deployments/{deployment_id}/logs",
    response_model=DeploymentLogsResponse,
    tags=["deployments"],
)
async def get_deployment_logs(
    deployment_id: uuid.UUID,
    session: SessionDep,
    tail: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> DeploymentLogsResponse:
    deployment = await get_deployment_or_404(session, deployment_id)
    return deployment_logs_response(deployment, tail)


@router.get(
    "/deployments/{deployment_id}/logs/stream",
    response_class=StreamingResponse,
    tags=["deployments"],
)
async def stream_deployment_logs(
    deployment_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    tail: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> StreamingResponse:
    await get_deployment_or_404(session, deployment_id)
    completed_statuses = {
        DeploymentStatus.FAILED,
        DeploymentStatus.STOPPED,
        DeploymentStatus.CANCELLED,
    }

    async def events() -> AsyncIterator[str]:
        previous = ""
        unchanged_ticks = 0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            session.expire_all()
            result = await session.execute(select(Deployment).where(Deployment.id == deployment_id))
            deployment = result.scalar_one_or_none()
            if deployment is None:
                yield "event: complete\ndata: {}\n\n"
                return

            payload = deployment_logs_response(deployment, tail).model_dump(mode="json")
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if serialized != previous:
                yield f"data: {serialized}\n\n"
                previous = serialized
                unchanged_ticks = 0
            else:
                unchanged_ticks += 1
                if unchanged_ticks >= 15:
                    yield ": keep-alive\n\n"
                    unchanged_ticks = 0

            if deployment.status in completed_statuses:
                yield "event: complete\ndata: {}\n\n"
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["projects"],
)
async def delete_project(project_id: uuid.UUID, session: SessionDep) -> Response:
    project = await get_project_or_404(session, project_id)
    ensure_lifecycle_idle(project)
    active = await session.execute(
        select(Deployment.id).where(
            Deployment.project_id == project_id,
            Deployment.status.in_(IN_PROGRESS_STATUSES),
        )
    )
    if active.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Wait for the active deployment to finish")
    if project.state is ProjectState.DELETING:
        raise HTTPException(status_code=409, detail="Project is already being deleted")
    project.state = ProjectState.DELETING
    project.cleanup_error = None
    await session.commit()
    try:
        enqueue_project_cleanup(project.id)
    except Exception as exc:
        project.state = ProjectState.ACTIVE
        project.cleanup_error = "Could not enqueue the cleanup task"
        await session.commit()
        raise HTTPException(status_code=503, detail=project.cleanup_error) from exc
    return Response(status_code=status.HTTP_202_ACCEPTED)
