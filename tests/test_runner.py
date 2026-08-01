import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.enums import DeploymentStatus, LifecycleState
from app.models import Deployment, Project, ProjectVariable
from app.services.deployment_runner import DeploymentRunner, collect_runtime_logs
from app.services.git_source import CloneResult
from app.services.project_lifecycle import rollback_project, start_project, stop_project
from app.services.secrets import SecretCipher


class FakeSource:
    def __init__(self) -> None:
        self.destination: Path | None = None

    def clone(self, repo_url: str, branch: str | None, destination: Path) -> CloneResult:
        del repo_url, branch
        self.destination = destination
        destination.mkdir(parents=True)
        (destination / "Dockerfile").write_text("FROM scratch", encoding="utf-8")
        return CloneResult(commit_sha="a" * 40, log="cloned\n")


class FakeRuntime:
    def __init__(
        self,
        *,
        fail_build: bool = False,
        fail_start: bool = False,
        fail_wait_for: set[str] | None = None,
        before_build_log: Callable[[], None] | None = None,
        after_start: Callable[[], None] | None = None,
    ) -> None:
        self.fail_build = fail_build
        self.fail_start = fail_start
        self.stopped: list[str] = []
        self.restarted: list[str] = []
        self.builds = 0
        self.removed_candidate = 0
        self.container_state = "running"
        self.environment: dict[str, str] = {}
        self.fail_wait_for = fail_wait_for or set()
        self.before_build_log = before_build_log
        self.after_start = after_start
        self.starts = 0

    def remove_candidate(self, project: Project, deployment: Deployment) -> None:
        del project, deployment
        self.removed_candidate += 1

    def build_image(
        self,
        context_path: Path,
        dockerfile_path: str,
        image_tag: str,
        project_id: uuid.UUID,
        on_log: Callable[[str], None],
    ) -> None:
        del context_path, dockerfile_path, image_tag, project_id
        self.builds += 1
        if self.fail_build:
            raise RuntimeError("build failed")
        if self.before_build_log is not None:
            self.before_build_log()
        on_log("built\n")

    def start(
        self,
        project: Project,
        deployment: Deployment,
        image_tag: str,
        environment: dict[str, str],
    ) -> tuple[str, str]:
        del project, deployment, image_tag
        self.starts += 1
        self.environment = environment
        if self.fail_start:
            raise RuntimeError("start failed")
        if self.after_start is not None:
            self.after_start()
        return "new-container", "deployforge-example-new"

    def wait_ready(
        self,
        container_id: str,
        image_tag: str,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        del image_tag
        if check_cancelled is not None:
            check_cancelled()
        if container_id in self.fail_wait_for:
            raise RuntimeError("readiness failed")

    def stop(self, container_id: str | None) -> None:
        if container_id:
            self.stopped.append(container_id)

    def restart(self, container_id: str | None) -> None:
        if container_id:
            self.restarted.append(container_id)

    def logs(self, container_id: str | None, tail: int) -> str:
        del container_id, tail
        return "runtime log"

    def state(self, container_id: str) -> tuple[str, int | None]:
        del container_id
        return self.container_state, 1 if self.container_state != "running" else 0

    def cleanup_project(self, project_id: uuid.UUID) -> None:
        del project_id


def make_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def seed(
    session: Session, *, previous: bool = False
) -> tuple[Project, Deployment, Deployment | None]:
    project = Project(
        name="Example",
        slug="example",
        repo_url="https://github.com/example/project",
        dockerfile_path="Dockerfile",
        container_port=8080,
    )
    session.add(project)
    session.flush()
    old = None
    if previous:
        old = Deployment(
            project_id=project.id,
            task_id="old-task",
            status=DeploymentStatus.RUNNING,
            container_id="old-container",
        )
        session.add(old)
    deployment = Deployment(
        project_id=project.id,
        task_id="new-task",
        status=DeploymentStatus.QUEUED,
        active_token="active",
    )
    session.add(deployment)
    session.commit()
    return project, deployment, old


def test_successful_deploy_stops_previous_and_cleans_temp_directory() -> None:
    session = make_session()
    project, deployment, previous = seed(session, previous=True)
    source = FakeSource()
    runtime = FakeRuntime()
    settings = Settings(secret_key="test-secret-key-for-deployforge")
    session.add(
        ProjectVariable(
            project_id=project.id,
            key="API_TOKEN",
            encrypted_value=SecretCipher(settings.secret_key).encrypt("worker-secret"),
            is_secret=True,
        )
    )
    session.commit()
    runner = DeploymentRunner(session, settings, source, runtime)
    runner.run(deployment.id)

    session.refresh(deployment)
    assert deployment.status is DeploymentStatus.RUNNING
    assert deployment.commit_sha == "a" * 40
    assert deployment.image_tag == "deployforge/example:" + "a" * 12
    assert deployment.container_id == "new-container"
    assert runtime.stopped == ["old-container"]
    assert runtime.environment == {"API_TOKEN": "worker-secret"}
    assert previous is not None
    session.refresh(previous)
    assert previous.status is DeploymentStatus.STOPPED
    assert source.destination is not None
    assert not source.destination.parent.exists()


def test_failed_start_restores_previous_container() -> None:
    session = make_session()
    _, deployment, previous = seed(session, previous=True)
    runtime = FakeRuntime(fail_start=True)
    runner = DeploymentRunner(session, Settings(), FakeSource(), runtime)
    runner.run(deployment.id)

    session.refresh(deployment)
    assert deployment.status is DeploymentStatus.FAILED
    assert deployment.active_token is None
    assert "start failed" in (deployment.error_message or "")
    assert runtime.stopped == ["old-container"]
    assert runtime.restarted == ["old-container"]
    assert previous is not None
    session.refresh(previous)
    assert previous.status is DeploymentStatus.RUNNING


def test_failed_build_preserves_previous_container_and_cleans_temp() -> None:
    session = make_session()
    _, deployment, previous = seed(session, previous=True)
    source = FakeSource()
    runtime = FakeRuntime(fail_build=True)
    DeploymentRunner(session, Settings(), source, runtime).run(deployment.id)
    session.refresh(deployment)
    assert deployment.status is DeploymentStatus.FAILED
    assert runtime.stopped == []
    assert runtime.restarted == []
    assert previous is not None
    session.refresh(previous)
    assert previous.status is DeploymentStatus.RUNNING
    assert source.destination is not None
    assert not source.destination.parent.exists()


def test_terminal_task_is_idempotent() -> None:
    session = make_session()
    _, deployment, _ = seed(session)
    deployment.status = DeploymentStatus.RUNNING
    deployment.active_token = None
    session.commit()
    runtime = FakeRuntime()
    DeploymentRunner(session, Settings(), FakeSource(), runtime).run(deployment.id)
    assert runtime.builds == 0


def test_interrupted_task_removes_candidate_before_retry() -> None:
    session = make_session()
    _, deployment, _ = seed(session)
    deployment.status = DeploymentStatus.BUILDING
    session.commit()
    runtime = FakeRuntime()
    DeploymentRunner(session, Settings(), FakeSource(), runtime).run(deployment.id)
    assert runtime.removed_candidate == 1
    assert deployment.status is DeploymentStatus.RUNNING


def test_cancellation_during_build_cleans_candidate_and_temp_directory() -> None:
    session = make_session()
    _, deployment, _ = seed(session)
    source = FakeSource()

    def request_cancel() -> None:
        deployment.cancel_requested = True
        session.commit()

    runtime = FakeRuntime(before_build_log=request_cancel)
    DeploymentRunner(session, Settings(), source, runtime).run(deployment.id)

    session.refresh(deployment)
    assert deployment.status is DeploymentStatus.CANCELLED
    assert deployment.active_token is None
    assert deployment.cancel_requested is True
    assert "cancelled by user" in deployment.build_log
    assert runtime.starts == 0
    assert runtime.removed_candidate == 1
    assert source.destination is not None
    assert not source.destination.parent.exists()


def test_cancellation_during_start_restores_previous_container() -> None:
    session = make_session()
    _, deployment, previous = seed(session, previous=True)

    def request_cancel() -> None:
        deployment.cancel_requested = True
        session.commit()

    runtime = FakeRuntime(after_start=request_cancel)
    DeploymentRunner(session, Settings(), FakeSource(), runtime).run(deployment.id)

    session.refresh(deployment)
    assert deployment.status is DeploymentStatus.CANCELLED
    assert deployment.container_id is None
    assert runtime.stopped == ["old-container", "new-container"]
    assert runtime.restarted == ["old-container"]
    assert runtime.removed_candidate == 1
    assert previous is not None
    session.refresh(previous)
    assert previous.status is DeploymentStatus.RUNNING


def test_log_collector_marks_exited_container_failed() -> None:
    session = make_session()
    _, deployment, _ = seed(session)
    deployment.status = DeploymentStatus.RUNNING
    deployment.active_token = None
    deployment.container_id = "container"
    session.commit()
    runtime = FakeRuntime()
    runtime.container_state = "exited"
    collect_runtime_logs(session, Settings(), runtime)
    session.refresh(deployment)
    assert deployment.status is DeploymentStatus.FAILED
    assert deployment.runtime_log == "runtime log"
    assert "code 1" in (deployment.error_message or "")


def seed_lifecycle(session: Session) -> tuple[Project, Deployment, Deployment]:
    project = Project(
        name="Lifecycle",
        slug="lifecycle",
        repo_url="https://github.com/example/lifecycle",
        dockerfile_path="Dockerfile",
        container_port=8080,
    )
    session.add(project)
    session.flush()
    current = Deployment(
        project_id=project.id,
        task_id="current-task",
        status=DeploymentStatus.RUNNING,
        container_id="current-container",
        image_tag="deployforge/lifecycle:current",
    )
    previous = Deployment(
        project_id=project.id,
        task_id="previous-task",
        status=DeploymentStatus.STOPPED,
        container_id="previous-container",
        image_tag="deployforge/lifecycle:previous",
    )
    session.add_all([current, previous])
    session.commit()
    return project, current, previous


def test_project_stop_and_repeated_delivery_are_idempotent() -> None:
    session = make_session()
    project, current, _ = seed_lifecycle(session)
    project.lifecycle_state = LifecycleState.STOPPING
    session.commit()
    runtime = FakeRuntime()

    stop_project(session, Settings(), runtime, project.id)
    session.refresh(project)
    session.refresh(current)
    assert project.lifecycle_state is LifecycleState.STOPPED
    assert current.status is DeploymentStatus.STOPPED
    assert current.runtime_log == "runtime log"
    assert runtime.stopped == ["current-container"]

    stop_project(session, Settings(), runtime, project.id)
    assert runtime.stopped == ["current-container"]


def test_project_start_restores_most_recently_stopped_deployment() -> None:
    session = make_session()
    project, current, previous = seed_lifecycle(session)
    current.status = DeploymentStatus.STOPPED
    current.finished_at = current.created_at
    previous.finished_at = None
    project.lifecycle_state = LifecycleState.STARTING
    session.commit()
    runtime = FakeRuntime()

    start_project(session, Settings(), runtime, project.id)
    session.refresh(project)
    session.refresh(current)
    assert runtime.restarted == ["current-container"]
    assert current.status is DeploymentStatus.RUNNING
    assert project.lifecycle_state is LifecycleState.ACTIVE


def test_failed_project_start_remains_stopped() -> None:
    session = make_session()
    project, current, _ = seed_lifecycle(session)
    current.status = DeploymentStatus.STOPPED
    current.finished_at = current.created_at
    project.lifecycle_state = LifecycleState.STARTING
    session.commit()
    runtime = FakeRuntime(fail_wait_for={"current-container"})

    start_project(session, Settings(), runtime, project.id)
    session.refresh(project)
    session.refresh(current)
    assert runtime.restarted == ["current-container"]
    assert runtime.stopped == ["current-container"]
    assert current.status is DeploymentStatus.STOPPED
    assert project.lifecycle_state is LifecycleState.STOPPED
    assert "readiness failed" in (project.operation_error or "")


def test_successful_manual_rollback_switches_running_deployment() -> None:
    session = make_session()
    project, current, previous = seed_lifecycle(session)
    project.lifecycle_state = LifecycleState.ROLLING_BACK
    session.commit()
    runtime = FakeRuntime()

    rollback_project(session, Settings(), runtime, project.id, previous.id)
    session.refresh(project)
    session.refresh(current)
    session.refresh(previous)
    assert runtime.stopped == ["current-container"]
    assert runtime.restarted == ["previous-container"]
    assert current.status is DeploymentStatus.STOPPED
    assert previous.status is DeploymentStatus.RUNNING
    assert project.lifecycle_state is LifecycleState.ACTIVE


def test_failed_manual_rollback_restores_current_deployment() -> None:
    session = make_session()
    project, current, previous = seed_lifecycle(session)
    project.lifecycle_state = LifecycleState.ROLLING_BACK
    session.commit()
    runtime = FakeRuntime(fail_wait_for={"previous-container"})

    rollback_project(session, Settings(), runtime, project.id, previous.id)
    session.refresh(project)
    session.refresh(current)
    session.refresh(previous)
    assert runtime.stopped == ["current-container", "previous-container"]
    assert runtime.restarted == ["previous-container", "current-container"]
    assert current.status is DeploymentStatus.RUNNING
    assert previous.status is DeploymentStatus.STOPPED
    assert project.lifecycle_state is LifecycleState.ACTIVE
    assert "readiness failed" in (project.operation_error or "")
