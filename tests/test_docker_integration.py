import os
import uuid
from contextlib import suppress
from pathlib import Path

import docker
import pytest

from app.config import Settings
from app.enums import DeploymentStatus
from app.models import Deployment, Project
from app.services.container_runtime import DockerRuntime


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("DEPLOYFORGE_RUN_DOCKER_TESTS") != "1",
    reason="set DEPLOYFORGE_RUN_DOCKER_TESTS=1 to run Docker integration tests",
)
def test_demo_image_builds_and_starts() -> None:
    client = docker.from_env()
    client.ping()
    project_id = uuid.uuid4()
    network_name = f"deployforge-test-{project_id.hex[:8]}"
    client.networks.create(network_name)
    settings = Settings(runtime_network=network_name, startup_timeout_seconds=30)
    runtime = DockerRuntime(settings)
    project = Project(
        id=project_id,
        name="Demo",
        slug=f"demo-{project_id.hex[:8]}",
        repo_url="https://github.com/example/demo",
        dockerfile_path="Dockerfile",
        container_port=8080,
    )
    deployment = Deployment(
        id=uuid.uuid4(),
        project_id=project.id,
        task_id=f"test-{project_id}",
        status=DeploymentStatus.BUILDING,
    )
    image_tag = f"deployforge/{project.slug}:test"
    log: list[str] = []
    container_id: str | None = None
    try:
        runtime.build_image(
            Path("examples/demo-app").resolve(),
            "Dockerfile",
            image_tag,
            project.id,
            log.append,
        )
        container_id, _ = runtime.start(
            project, deployment, image_tag, {"DEPLOYFORGE_TEST_VALUE": "available"}
        )
        runtime.wait_ready(container_id, image_tag)
        state, _ = runtime.state(container_id)
        container = client.containers.get(container_id)
        container.reload()
        labels = container.attrs["Config"]["Labels"]
        assert state == "running"
        assert container.attrs["HostConfig"]["NetworkMode"] == network_name
        assert labels["traefik.docker.network"] == network_name
        assert "DEPLOYFORGE_TEST_VALUE=available" in container.attrs["Config"]["Env"]
        assert log
    finally:
        if container_id:
            runtime.stop(container_id)
        runtime.cleanup_project(project.id)
        with suppress(docker.errors.NotFound):
            client.networks.get(network_name).remove()
