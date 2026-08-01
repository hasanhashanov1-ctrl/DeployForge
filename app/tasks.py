import logging
import uuid

from app.celery_app import celery_app
from app.config import get_settings
from app.database import SyncSessionLocal
from app.enums import DeploymentStatus, LifecycleState, ProjectState
from app.models import Deployment, Project
from app.services.container_runtime import DockerRuntime
from app.services.deployment_runner import DeploymentRunner, collect_runtime_logs
from app.services.git_source import SubprocessGitSource
from app.services.project_lifecycle import mark_operation_failed
from app.services.project_lifecycle import rollback_project as run_rollback
from app.services.project_lifecycle import start_project as run_start
from app.services.project_lifecycle import stop_project as run_stop

logger = logging.getLogger(__name__)


@celery_app.task(name="deployforge.deploy_project")
def deploy_project(deployment_id: str) -> None:
    settings = get_settings()
    with SyncSessionLocal() as session:
        identifier = uuid.UUID(deployment_id)
        try:
            runtime = DockerRuntime(settings)
        except Exception as exc:
            deployment = session.get(Deployment, identifier)
            if deployment is not None and deployment.status not in {
                DeploymentStatus.RUNNING,
                DeploymentStatus.FAILED,
                DeploymentStatus.STOPPED,
                DeploymentStatus.CANCELLED,
            }:
                deployment.status = DeploymentStatus.FAILED
                deployment.active_token = None
                deployment.error_message = str(exc)[:2000]
                session.commit()
            logger.exception("docker_unavailable_for_deployment")
            return
        DeploymentRunner(
            session=session,
            settings=settings,
            source=SubprocessGitSource(settings.clone_timeout_seconds),
            runtime=runtime,
        ).run(identifier)


@celery_app.task(name="deployforge.collect_runtime_logs")
def collect_logs_task() -> None:
    settings = get_settings()
    try:
        runtime = DockerRuntime(settings)
    except Exception:
        logger.exception("docker_unavailable_during_log_collection")
        return
    with SyncSessionLocal() as session:
        collect_runtime_logs(session, settings, runtime)


@celery_app.task(name="deployforge.cleanup_project")
def cleanup_project(project_id: str) -> None:
    identifier = uuid.UUID(project_id)
    settings = get_settings()
    with SyncSessionLocal() as session:
        project = session.get(Project, identifier)
        if project is None:
            return
        try:
            runtime = DockerRuntime(settings)
            runtime.cleanup_project(project.id)
            for deployment in project.deployments:
                if deployment.status == DeploymentStatus.RUNNING:
                    deployment.status = DeploymentStatus.STOPPED
            session.delete(project)
            session.commit()
        except Exception as exc:
            session.rollback()
            project = session.get(Project, identifier)
            if project is not None:
                project.state = ProjectState.ACTIVE
                project.cleanup_error = str(exc)[:2000]
                session.commit()
            logger.exception("project_cleanup_failed", extra={"project_id": project_id})


def _run_lifecycle_task(
    project_id: str,
    fallback: LifecycleState,
    operation: str,
    target_id: str | None = None,
) -> None:
    identifier = uuid.UUID(project_id)
    settings = get_settings()
    with SyncSessionLocal() as session:
        try:
            runtime = DockerRuntime(settings)
            if operation == "stop":
                run_stop(session, settings, runtime, identifier)
            elif operation == "start":
                run_start(session, settings, runtime, identifier)
            elif target_id is not None:
                run_rollback(session, settings, runtime, identifier, uuid.UUID(target_id))
        except Exception as exc:
            mark_operation_failed(session, identifier, fallback, exc)
            logger.exception(
                "project_lifecycle_task_failed",
                extra={"project_id": project_id, "operation": operation},
            )


@celery_app.task(name="deployforge.stop_project")
def stop_project(project_id: str) -> None:
    _run_lifecycle_task(project_id, LifecycleState.ACTIVE, "stop")


@celery_app.task(name="deployforge.start_project")
def start_project(project_id: str) -> None:
    _run_lifecycle_task(project_id, LifecycleState.STOPPED, "start")


@celery_app.task(name="deployforge.rollback_project")
def rollback_project(project_id: str, deployment_id: str) -> None:
    _run_lifecycle_task(project_id, LifecycleState.ACTIVE, "rollback", deployment_id)
