import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import docker
from docker.errors import APIError, BuildError, ImageNotFound, NotFound
from docker.models.containers import Container

from app.config import Settings
from app.models import Deployment, Project


class ContainerRuntimeError(RuntimeError):
    pass


class ContainerRuntime(Protocol):
    def remove_candidate(self, project: Project, deployment: Deployment) -> None: ...

    def build_image(
        self,
        context_path: Path,
        dockerfile_path: str,
        image_tag: str,
        project_id: uuid.UUID,
        on_log: Callable[[str], None],
    ) -> None: ...

    def start(
        self,
        project: Project,
        deployment: Deployment,
        image_tag: str,
        environment: dict[str, str],
    ) -> tuple[str, str]: ...

    def wait_ready(
        self,
        container_id: str,
        image_tag: str,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None: ...

    def stop(self, container_id: str | None) -> None: ...

    def restart(self, container_id: str | None) -> None: ...

    def logs(self, container_id: str | None, tail: int) -> str: ...

    def state(self, container_id: str) -> tuple[str, int | None]: ...

    def cleanup_project(self, project_id: uuid.UUID) -> None: ...


class DockerRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as exc:
            raise ContainerRuntimeError("Docker Engine is unavailable to the worker") from exc

    @staticmethod
    def container_name(project: Project, deployment: Deployment) -> str:
        return f"deployforge-{project.slug}-{deployment.id.hex[:12]}"

    def remove_candidate(self, project: Project, deployment: Deployment) -> None:
        try:
            container = self.client.containers.get(self.container_name(project, deployment))
            container.remove(force=True)
        except NotFound:
            return
        except APIError as exc:
            raise ContainerRuntimeError(f"Cannot remove an unfinished candidate: {exc}") from exc

    def build_image(
        self,
        context_path: Path,
        dockerfile_path: str,
        image_tag: str,
        project_id: uuid.UUID,
        on_log: Callable[[str], None],
    ) -> None:
        try:
            stream = self.client.api.build(
                path=str(context_path),
                dockerfile=dockerfile_path,
                tag=image_tag,
                rm=True,
                forcerm=True,
                decode=True,
                labels={
                    "com.deployforge.managed": "true",
                    "com.deployforge.project_id": str(project_id),
                },
            )
            for event in stream:
                if "stream" in event:
                    on_log(str(event["stream"]))
                elif "status" in event:
                    detail = event.get("progress") or event.get("id") or ""
                    on_log(f"{event['status']} {detail}".rstrip() + "\n")
                elif "error" in event:
                    on_log(str(event["error"]) + "\n")
                    raise ContainerRuntimeError(str(event["error"]))
                else:
                    on_log(json.dumps(event, ensure_ascii=False) + "\n")
        except (APIError, BuildError) as exc:
            raise ContainerRuntimeError(f"Docker image build failed: {exc}") from exc

    def start(
        self,
        project: Project,
        deployment: Deployment,
        image_tag: str,
        environment: dict[str, str],
    ) -> tuple[str, str]:
        route_name = f"df-{deployment.id.hex[:12]}"
        labels = {
            "com.deployforge.managed": "true",
            "com.deployforge.project_id": str(project.id),
            "com.deployforge.deployment_id": str(deployment.id),
            "traefik.enable": "true",
            "traefik.docker.network": self.settings.runtime_network,
            f"traefik.http.routers.{route_name}.rule": f"Host(`{project.slug}.localhost`)",
            f"traefik.http.routers.{route_name}.entrypoints": "web",
            f"traefik.http.routers.{route_name}.service": route_name,
            f"traefik.http.services.{route_name}.loadbalancer.server.port": str(
                project.container_port
            ),
        }
        name = self.container_name(project, deployment)
        try:
            container: Container = self.client.containers.run(
                image_tag,
                detach=True,
                name=name,
                network=self.settings.runtime_network,
                labels=labels,
                environment=environment or None,
            )
        except (APIError, ImageNotFound) as exc:
            raise ContainerRuntimeError(f"Cannot start the application container: {exc}") from exc
        return container.id, name

    def wait_ready(
        self,
        container_id: str,
        image_tag: str,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        try:
            image = self.client.images.get(image_tag)
        except ImageNotFound as exc:
            raise ContainerRuntimeError("Built image disappeared before startup") from exc
        health_config = (image.attrs.get("Config") or {}).get("Healthcheck") or {}
        health_test = health_config.get("Test") or []
        has_healthcheck = bool(health_test and health_test != ["NONE"])
        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        grace_deadline = time.monotonic() + self.settings.startup_grace_seconds
        while time.monotonic() < deadline:
            if check_cancelled is not None:
                check_cancelled()
            try:
                container = self.client.containers.get(container_id)
                container.reload()
            except NotFound as exc:
                raise ContainerRuntimeError(
                    "Application container disappeared during startup"
                ) from exc
            state = container.attrs.get("State", {})
            if not state.get("Running", False):
                code = state.get("ExitCode")
                raise ContainerRuntimeError(f"Application container exited with code {code}")
            if has_healthcheck:
                health = state.get("Health", {}).get("Status")
                if health == "healthy":
                    return
                if health == "unhealthy":
                    raise ContainerRuntimeError(
                        "Application container reported an unhealthy status"
                    )
            elif time.monotonic() >= grace_deadline:
                return
            time.sleep(1)
        raise ContainerRuntimeError(
            "Application did not become ready within "
            f"{self.settings.startup_timeout_seconds} seconds"
        )

    def stop(self, container_id: str | None) -> None:
        if not container_id:
            return
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=10)
        except NotFound:
            return
        except APIError as exc:
            raise ContainerRuntimeError(
                f"Cannot stop container {container_id[:12]}: {exc}"
            ) from exc

    def restart(self, container_id: str | None) -> None:
        if not container_id:
            return
        try:
            container = self.client.containers.get(container_id)
            container.restart(timeout=10)
        except NotFound as exc:
            raise ContainerRuntimeError("Previous application container no longer exists") from exc
        except APIError as exc:
            raise ContainerRuntimeError(f"Cannot restore previous application: {exc}") from exc

    def logs(self, container_id: str | None, tail: int) -> str:
        if not container_id:
            return ""
        try:
            container = self.client.containers.get(container_id)
            output: bytes = container.logs(tail=tail, timestamps=True)
            return output.decode("utf-8", errors="replace")
        except NotFound:
            return ""
        except APIError as exc:
            raise ContainerRuntimeError(f"Cannot read application logs: {exc}") from exc

    def state(self, container_id: str) -> tuple[str, int | None]:
        try:
            container = self.client.containers.get(container_id)
            container.reload()
        except NotFound:
            return "missing", None
        state = container.attrs.get("State", {})
        return str(state.get("Status", "unknown")), state.get("ExitCode")

    def cleanup_project(self, project_id: uuid.UUID) -> None:
        label = f"com.deployforge.project_id={project_id}"
        try:
            for container in self.client.containers.list(all=True, filters={"label": label}):
                container.remove(force=True)
            for image in self.client.images.list(filters={"label": label}):
                try:
                    self.client.images.remove(image.id, force=True)
                except ImageNotFound:
                    continue
        except APIError as exc:
            raise ContainerRuntimeError(f"Cannot clean project Docker resources: {exc}") from exc
