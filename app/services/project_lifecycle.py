import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import DeploymentStatus, LifecycleState
from app.models import Deployment, Project
from app.services.container_runtime import ContainerRuntime
from app.services.log_buffer import bounded_text

logger = logging.getLogger(__name__)


def _deployment_logs(runtime: ContainerRuntime, settings: Settings, deployment: Deployment) -> None:
    deployment.runtime_log, deployment.runtime_log_truncated = bounded_text(
        runtime.logs(deployment.container_id, settings.runtime_log_tail),
        settings.runtime_log_limit_bytes,
    )


def _running(session: Session, project_id: uuid.UUID) -> Deployment | None:
    return session.execute(
        select(Deployment)
        .where(
            Deployment.project_id == project_id,
            Deployment.status == DeploymentStatus.RUNNING,
        )
        .order_by(desc(Deployment.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _last_stopped(session: Session, project_id: uuid.UUID) -> Deployment | None:
    return session.execute(
        select(Deployment)
        .where(
            Deployment.project_id == project_id,
            Deployment.status == DeploymentStatus.STOPPED,
            Deployment.container_id.is_not(None),
            Deployment.image_tag.is_not(None),
        )
        .order_by(desc(Deployment.finished_at), desc(Deployment.created_at))
        .limit(1)
    ).scalar_one_or_none()


def mark_operation_failed(
    session: Session,
    project_id: uuid.UUID,
    fallback: LifecycleState,
    error: Exception,
) -> None:
    session.rollback()
    project = session.get(Project, project_id)
    if project is None:
        return
    project.lifecycle_state = fallback
    project.operation_error = str(error)[:2000]
    session.commit()


def stop_project(
    session: Session,
    settings: Settings,
    runtime: ContainerRuntime,
    project_id: uuid.UUID,
) -> None:
    project = session.get(Project, project_id)
    if project is None or project.lifecycle_state is not LifecycleState.STOPPING:
        return
    deployment = _running(session, project_id)
    if deployment is None:
        project.lifecycle_state = LifecycleState.STOPPED
        project.operation_error = None
        session.commit()
        return
    try:
        runtime.stop(deployment.container_id)
        _deployment_logs(runtime, settings, deployment)
        deployment.status = DeploymentStatus.STOPPED
        deployment.finished_at = datetime.now(UTC)
        project.lifecycle_state = LifecycleState.STOPPED
        project.operation_error = None
        session.commit()
    except Exception as exc:
        logger.exception("project_stop_failed", extra={"project_id": str(project_id)})
        mark_operation_failed(session, project_id, LifecycleState.ACTIVE, exc)


def start_project(
    session: Session,
    settings: Settings,
    runtime: ContainerRuntime,
    project_id: uuid.UUID,
) -> None:
    project = session.get(Project, project_id)
    if project is None or project.lifecycle_state is not LifecycleState.STARTING:
        return
    deployment = _last_stopped(session, project_id)
    if deployment is None or deployment.container_id is None or deployment.image_tag is None:
        mark_operation_failed(
            session,
            project_id,
            LifecycleState.STOPPED,
            RuntimeError("No stopped deployment can be started"),
        )
        return
    try:
        runtime.restart(deployment.container_id)
        runtime.wait_ready(deployment.container_id, deployment.image_tag)
        deployment.status = DeploymentStatus.RUNNING
        deployment.finished_at = None
        deployment.error_message = None
        project.lifecycle_state = LifecycleState.ACTIVE
        project.operation_error = None
        session.commit()
    except Exception as exc:
        try:
            runtime.stop(deployment.container_id)
        except Exception:
            logger.exception("failed_start_cleanup_failed")
        logger.exception("project_start_failed", extra={"project_id": str(project_id)})
        mark_operation_failed(session, project_id, LifecycleState.STOPPED, exc)


def rollback_project(
    session: Session,
    settings: Settings,
    runtime: ContainerRuntime,
    project_id: uuid.UUID,
    target_id: uuid.UUID,
) -> None:
    project = session.get(Project, project_id)
    if project is None or project.lifecycle_state is not LifecycleState.ROLLING_BACK:
        return
    current = _running(session, project_id)
    target = session.get(Deployment, target_id)
    if (
        current is None
        or target is None
        or target.project_id != project_id
        or target.status is not DeploymentStatus.STOPPED
        or target.container_id is None
        or target.image_tag is None
    ):
        mark_operation_failed(
            session,
            project_id,
            LifecycleState.ACTIVE,
            RuntimeError("Rollback target is no longer available"),
        )
        return

    current_stopped = False
    try:
        runtime.stop(current.container_id)
        current_stopped = True
        _deployment_logs(runtime, settings, current)
        runtime.restart(target.container_id)
        runtime.wait_ready(target.container_id, target.image_tag)
        current.status = DeploymentStatus.STOPPED
        current.finished_at = datetime.now(UTC)
        target.status = DeploymentStatus.RUNNING
        target.finished_at = None
        target.error_message = None
        project.lifecycle_state = LifecycleState.ACTIVE
        project.operation_error = None
        session.commit()
    except Exception as exc:
        logger.exception("project_rollback_failed", extra={"project_id": str(project_id)})
        try:
            runtime.stop(target.container_id)
        except Exception:
            logger.exception("rollback_target_stop_failed")
        restore_error: Exception | None = None
        if current_stopped and current.container_id and current.image_tag:
            try:
                runtime.restart(current.container_id)
                runtime.wait_ready(current.container_id, current.image_tag)
            except Exception as recovery_exc:
                restore_error = recovery_exc
                logger.exception("rollback_current_restore_failed")
        message = str(exc)
        if restore_error is not None:
            message = f"{message}; current version restore failed: {restore_error}"
        fallback = LifecycleState.STOPPED if restore_error is not None else LifecycleState.ACTIVE
        mark_operation_failed(session, project_id, fallback, RuntimeError(message))
        if restore_error is not None:
            failed_current = session.get(Deployment, current.id)
            if failed_current is not None:
                failed_current.status = DeploymentStatus.FAILED
                failed_current.error_message = str(restore_error)[:2000]
                failed_current.finished_at = datetime.now(UTC)
                session.commit()
