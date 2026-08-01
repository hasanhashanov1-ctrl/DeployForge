import logging
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import DeploymentStatus, LifecycleState, ProjectState
from app.models import Deployment, Project, ProjectVariable
from app.services.container_runtime import ContainerRuntime
from app.services.git_source import GitSource
from app.services.log_buffer import LogBuffer, bounded_text
from app.services.secrets import SecretCipher

logger = logging.getLogger(__name__)


class DeploymentCancelled(RuntimeError):
    pass


class DeploymentRunner:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        source: GitSource,
        runtime: ContainerRuntime,
    ) -> None:
        self.session = session
        self.settings = settings
        self.source = source
        self.runtime = runtime
        self._last_build_log_flush = 0.0

    def _save_status(
        self, deployment: Deployment, status: DeploymentStatus, buffer: LogBuffer
    ) -> None:
        deployment.status = status
        deployment.build_log = buffer.value
        deployment.build_log_truncated = buffer.truncated
        self.session.commit()

    def _check_cancelled(self, deployment: Deployment) -> None:
        self.session.refresh(deployment, attribute_names=["cancel_requested"])
        if deployment.cancel_requested:
            raise DeploymentCancelled("Deployment cancelled by user")

    def _previous_running(self, deployment: Deployment) -> Deployment | None:
        return self.session.execute(
            select(Deployment)
            .where(
                Deployment.project_id == deployment.project_id,
                Deployment.id != deployment.id,
                Deployment.status == DeploymentStatus.RUNNING,
            )
            .order_by(desc(Deployment.created_at))
            .limit(1)
        ).scalar_one_or_none()

    def _environment(self, project_id: uuid.UUID) -> dict[str, str]:
        cipher = SecretCipher(self.settings.secret_key)
        variables = self.session.execute(
            select(ProjectVariable)
            .where(ProjectVariable.project_id == project_id)
            .order_by(ProjectVariable.key)
        ).scalars()
        return {item.key: cipher.decrypt(item.encrypted_value) for item in variables}

    def run(self, deployment_id: uuid.UUID) -> None:
        deployment = self.session.get(Deployment, deployment_id)
        if deployment is None or deployment.status in {
            DeploymentStatus.RUNNING,
            DeploymentStatus.FAILED,
            DeploymentStatus.STOPPED,
            DeploymentStatus.CANCELLED,
        }:
            return
        project = self.session.get(Project, deployment.project_id)
        if project is None or project.state is ProjectState.DELETING:
            self._fail(
                deployment,
                LogBuffer(self.settings.build_log_limit_bytes),
                "Project unavailable",
            )
            return

        buffer = LogBuffer(self.settings.build_log_limit_bytes, deployment.build_log)
        previous = self._previous_running(deployment)
        previous_stopped = False
        candidate_id: str | None = None
        try:
            self._check_cancelled(deployment)
            if deployment.status is not DeploymentStatus.QUEUED:
                buffer.append("Retrying an interrupted deployment\n")
                self.runtime.remove_candidate(project, deployment)
                if previous and previous.container_id:
                    previous_state, _ = self.runtime.state(previous.container_id)
                    if previous_state != "running":
                        self.runtime.restart(previous.container_id)

            deployment.started_at = deployment.started_at or datetime.now(UTC)
            self._save_status(deployment, DeploymentStatus.CLONING, buffer)
            self._check_cancelled(deployment)
            with tempfile.TemporaryDirectory(prefix="deployforge-") as temporary:
                destination = Path(temporary) / "repository"
                clone = self.source.clone(project.repo_url, project.branch, destination)
                buffer.append(clone.log)
                self._check_cancelled(deployment)
                deployment.commit_sha = clone.commit_sha
                dockerfile = destination / project.dockerfile_path
                if not dockerfile.is_file():
                    raise RuntimeError(f"Dockerfile not found: {project.dockerfile_path}")

                image_tag = f"deployforge/{project.slug}:{clone.commit_sha[:12]}"
                deployment.image_tag = image_tag
                self._save_status(deployment, DeploymentStatus.BUILDING, buffer)
                self.runtime.build_image(
                    destination,
                    project.dockerfile_path,
                    image_tag,
                    project.id,
                    lambda message: self._append_build_log(deployment, buffer, message),
                )
                self._check_cancelled(deployment)
                deployment.build_log = buffer.value
                deployment.build_log_truncated = buffer.truncated
                self.session.commit()

            environment = self._environment(project.id)
            self._check_cancelled(deployment)
            previous = self._previous_running(deployment)
            if previous and previous.container_id:
                self.runtime.stop(previous.container_id)
                previous_stopped = True
                previous.runtime_log, previous.runtime_log_truncated = bounded_text(
                    self.runtime.logs(previous.container_id, self.settings.runtime_log_tail),
                    self.settings.runtime_log_limit_bytes,
                )

            self._save_status(deployment, DeploymentStatus.STARTING, buffer)
            self._check_cancelled(deployment)
            candidate_id, candidate_name = self.runtime.start(
                project, deployment, image_tag, environment
            )
            deployment.container_id = candidate_id
            deployment.container_name = candidate_name
            self.session.commit()
            self._check_cancelled(deployment)
            self.runtime.wait_ready(
                candidate_id,
                image_tag,
                lambda: self._check_cancelled(deployment),
            )
            self._check_cancelled(deployment)

            if previous:
                previous.status = DeploymentStatus.STOPPED
                previous.finished_at = datetime.now(UTC)
            deployment.status = DeploymentStatus.RUNNING
            deployment.active_token = None
            deployment.finished_at = datetime.now(UTC)
            deployment.error_message = None
            deployment.build_log = buffer.value
            deployment.build_log_truncated = buffer.truncated
            project.lifecycle_state = LifecycleState.ACTIVE
            project.operation_error = None
            self.session.commit()
            logger.info("deployment_running", extra={"deployment_id": str(deployment.id)})
        except DeploymentCancelled:
            self._cancel(
                project,
                deployment,
                previous,
                previous_stopped,
                candidate_id,
                buffer,
            )
        except Exception as exc:
            try:
                self._check_cancelled(deployment)
            except DeploymentCancelled:
                self._cancel(
                    project,
                    deployment,
                    previous,
                    previous_stopped,
                    candidate_id,
                    buffer,
                )
                return
            logger.exception("deployment_failed", extra={"deployment_id": str(deployment.id)})
            if candidate_id:
                try:
                    self.runtime.stop(candidate_id)
                except Exception:
                    logger.exception("candidate_stop_failed")
            if previous_stopped and previous and previous.container_id:
                try:
                    self.runtime.restart(previous.container_id)
                    previous.status = DeploymentStatus.RUNNING
                    previous.error_message = None
                    previous.finished_at = None
                except Exception as restore_error:
                    buffer.append(f"Failed to restore previous container: {restore_error}\n")
            buffer.append(f"ERROR: {exc}\n")
            self._fail(deployment, buffer, str(exc))

    def _append_build_log(self, deployment: Deployment, buffer: LogBuffer, message: str) -> None:
        buffer.append(message)
        now = time.monotonic()
        if now - self._last_build_log_flush >= 0.5:
            deployment.build_log = buffer.value
            deployment.build_log_truncated = buffer.truncated
            self.session.commit()
            self._last_build_log_flush = now
            self._check_cancelled(deployment)

    def _cancel(
        self,
        project: Project,
        deployment: Deployment,
        previous: Deployment | None,
        previous_stopped: bool,
        candidate_id: str | None,
        buffer: LogBuffer,
    ) -> None:
        logger.info("deployment_cancelled", extra={"deployment_id": str(deployment.id)})
        if candidate_id:
            try:
                self.runtime.stop(candidate_id)
            except Exception as exc:
                buffer.append(f"Failed to stop cancelled container: {exc}\n")
        try:
            self.runtime.remove_candidate(project, deployment)
            deployment.container_id = None
            deployment.container_name = None
        except Exception as exc:
            buffer.append(f"Failed to remove cancelled container: {exc}\n")

        if previous_stopped and previous and previous.container_id:
            try:
                self.runtime.restart(previous.container_id)
                if previous.image_tag:
                    self.runtime.wait_ready(previous.container_id, previous.image_tag)
                previous.status = DeploymentStatus.RUNNING
                previous.error_message = None
                previous.finished_at = None
            except Exception as exc:
                previous.status = DeploymentStatus.FAILED
                previous.error_message = f"Restore after cancellation failed: {exc}"[:2000]
                previous.finished_at = datetime.now(UTC)
                project.lifecycle_state = LifecycleState.STOPPED
                project.operation_error = previous.error_message
                buffer.append(previous.error_message + "\n")

        buffer.append("Deployment cancelled by user\n")
        deployment.status = DeploymentStatus.CANCELLED
        deployment.active_token = None
        deployment.cancel_requested = True
        deployment.error_message = None
        deployment.build_log = buffer.value
        deployment.build_log_truncated = buffer.truncated
        deployment.finished_at = datetime.now(UTC)
        self.session.commit()

    def _fail(self, deployment: Deployment, buffer: LogBuffer, message: str) -> None:
        deployment.status = DeploymentStatus.FAILED
        deployment.active_token = None
        deployment.error_message = message[:2000]
        deployment.build_log = buffer.value
        deployment.build_log_truncated = buffer.truncated
        deployment.finished_at = datetime.now(UTC)
        self.session.commit()


def collect_runtime_logs(session: Session, settings: Settings, runtime: ContainerRuntime) -> None:
    deployments = session.execute(
        select(Deployment).where(Deployment.status == DeploymentStatus.RUNNING)
    ).scalars()
    for deployment in deployments:
        if not deployment.container_id:
            continue
        try:
            raw = runtime.logs(deployment.container_id, settings.runtime_log_tail)
            deployment.runtime_log, deployment.runtime_log_truncated = bounded_text(
                raw, settings.runtime_log_limit_bytes
            )
            container_state, exit_code = runtime.state(deployment.container_id)
            if container_state != "running":
                deployment.status = DeploymentStatus.FAILED
                deployment.error_message = f"Application container stopped with code {exit_code}"
                deployment.finished_at = datetime.now(UTC)
        except Exception as exc:
            logger.warning(
                "runtime_log_collection_failed",
                extra={"deployment_id": str(deployment.id), "error": str(exc)},
            )
    session.commit()
