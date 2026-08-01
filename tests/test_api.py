import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import DeploymentStatus, LifecycleState, ProjectState
from app.models import Deployment, Project, ProjectVariable

PROJECT = {
    "name": "Example API",
    "slug": "example-api",
    "repo_url": "https://github.com/example/project",
    "branch": None,
    "dockerfile_path": "Dockerfile",
    "container_port": 8080,
}


@pytest.mark.asyncio
async def test_dashboard_and_static_assets_are_served(client: AsyncClient) -> None:
    dashboard = await client.get("/")
    assert dashboard.status_code == 200
    assert "DeployForge" in dashboard.text
    assert 'id="project-list"' in dashboard.text
    assert 'id="edit-project-button"' in dashboard.text
    assert 'id="live-log-status"' in dashboard.text

    script = await client.get("/static/app.js")
    assert script.status_code == 200
    assert "loadProjects" in script.text


@pytest.mark.asyncio
async def test_project_crud_and_duplicate_slug(client: AsyncClient) -> None:
    created = await client.post("/projects", json=PROJECT)
    assert created.status_code == 201
    body = created.json()
    assert body["slug"] == "example-api"
    assert body["public_url"] == "http://example-api.localhost"
    assert body["latest_status"] is None
    assert body["lifecycle_state"] == "active"
    assert body["operation_error"] is None

    listed = await client.get("/projects")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    fetched = await client.get(f"/projects/{body['id']}")
    assert fetched.status_code == 200
    duplicate = await client.post("/projects", json=PROJECT)
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_invalid_project_returns_422(client: AsyncClient) -> None:
    invalid = {**PROJECT, "repo_url": "http://localhost/repository", "container_port": 0}
    response = await client.post("/projects", json=invalid)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_project_settings_can_be_updated_safely(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = (await client.post("/projects", json=PROJECT)).json()
    second = (
        await client.post(
            "/projects",
            json={**PROJECT, "name": "Second", "slug": "second-project"},
        )
    ).json()

    updated = await client.patch(
        f"/projects/{first['id']}",
        json={
            "name": "Renamed API",
            "slug": "renamed-api",
            "repo_url": "https://github.com/example/renamed",
            "branch": "release",
            "dockerfile_path": "docker/Dockerfile",
            "container_port": 9000,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed API"
    assert updated.json()["slug"] == "renamed-api"
    assert updated.json()["public_url"] == "http://renamed-api.localhost"
    assert updated.json()["branch"] == "release"

    cleared_branch = await client.patch(f"/projects/{first['id']}", json={"branch": None})
    assert cleared_branch.status_code == 200
    assert cleared_branch.json()["branch"] is None
    assert (await client.patch(f"/projects/{first['id']}", json={})).status_code == 422
    assert (
        await client.patch(f"/projects/{first['id']}", json={"dockerfile_path": "../Dockerfile"})
    ).status_code == 422
    assert (
        await client.patch(f"/projects/{second['id']}", json={"slug": "renamed-api"})
    ).status_code == 409

    deployment_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Deployment(
                id=deployment_id,
                project_id=uuid.UUID(first["id"]),
                task_id="settings-deployment",
                status=DeploymentStatus.QUEUED,
                active_token="active",
            )
        )
        await session.commit()

    assert (
        await client.patch(f"/projects/{first['id']}", json={"name": "Blocked"})
    ).status_code == 409

    async with session_factory() as session:
        deployment = await session.get(Deployment, deployment_id)
        assert deployment is not None
        deployment.status = DeploymentStatus.RUNNING
        deployment.active_token = None
        deployment.container_id = "settings-container"
        deployment.image_tag = "deployforge/renamed-api:abc"
        await session.commit()

    assert (
        await client.patch(f"/projects/{first['id']}", json={"slug": "new-route"})
    ).status_code == 409
    assert (
        await client.patch(f"/projects/{first['id']}", json={"container_port": 7000})
    ).status_code == 409
    repository_update = await client.patch(
        f"/projects/{first['id']}",
        json={"repo_url": "https://github.com/example/next-version"},
    )
    assert repository_update.status_code == 200
    assert repository_update.json()["latest_status"] == "running"


@pytest.mark.asyncio
async def test_project_variables_are_encrypted_masked_and_replaceable(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    project = (await client.post("/projects", json=PROJECT)).json()
    updated = await client.put(
        f"/projects/{project['id']}/variables",
        json={
            "variables": [
                {"key": "APP_MODE", "value": " production ", "is_secret": False},
                {"key": "API_TOKEN", "value": "super-secret", "is_secret": True},
            ]
        },
    )
    assert updated.status_code == 200
    assert updated.json() == [
        {"key": "API_TOKEN", "value": None, "is_secret": True, "has_value": True},
        {"key": "APP_MODE", "value": " production ", "is_secret": False, "has_value": True},
    ]

    async with session_factory() as session:
        result = await session.execute(
            select(ProjectVariable).where(ProjectVariable.key == "API_TOKEN")
        )
        stored = result.scalar_one()
        assert stored.encrypted_value != "super-secret"
        assert "super-secret" not in stored.encrypted_value

    preserved = await client.put(
        f"/projects/{project['id']}/variables",
        json={"variables": [{"key": "API_TOKEN", "value": None, "is_secret": True}]},
    )
    assert preserved.status_code == 200
    assert preserved.json()[0]["value"] is None
    listed = await client.get(f"/projects/{project['id']}/variables")
    assert listed.json() == preserved.json()

    invalid = await client.put(
        f"/projects/{project['id']}/variables",
        json={"variables": [{"key": "BAD-KEY", "value": "x", "is_secret": False}]},
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_deployment_conflict_history_and_logs(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    project = (await client.post("/projects", json=PROJECT)).json()
    first = await client.post(f"/projects/{project['id']}/deploy")
    assert first.status_code == 202
    deployment = first.json()
    assert deployment["status"] == "queued"

    conflict = await client.post(f"/projects/{project['id']}/deploy")
    assert conflict.status_code == 409
    variable_conflict = await client.put(
        f"/projects/{project['id']}/variables",
        json={"variables": [{"key": "APP_MODE", "value": "test", "is_secret": False}]},
    )
    assert variable_conflict.status_code == 409

    async with session_factory() as session:
        stored = await session.get(Deployment, uuid.UUID(deployment["id"]))
        assert stored is not None
        stored.status = DeploymentStatus.FAILED
        stored.active_token = None
        stored.build_log = "one\ntwo\nthree"
        stored.runtime_log = "alpha\nbeta"
        stored.error_message = "build failed"
        await session.commit()

    history = await client.get(f"/projects/{project['id']}/deployments")
    assert history.status_code == 200
    assert history.json()[0]["error_message"] == "build failed"
    logs = await client.get(f"/deployments/{deployment['id']}/logs?tail=2")
    assert logs.status_code == 200
    assert logs.json()["build_log"] == "two\nthree"
    assert logs.json()["runtime_log"] == "alpha\nbeta"
    stream = await client.get(f"/deployments/{deployment['id']}/logs/stream?tail=2")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert '"build_log":"two\\nthree"' in stream.text
    assert "event: complete" in stream.text


@pytest.mark.asyncio
async def test_stopped_project_can_queue_a_new_deployment(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    project = (await client.post("/projects", json=PROJECT)).json()
    async with session_factory() as session:
        stored = await session.get(Project, uuid.UUID(project["id"]))
        assert stored is not None
        stored.lifecycle_state = LifecycleState.STOPPED
        await session.commit()

    deployment = await client.post(f"/projects/{project['id']}/deploy")
    assert deployment.status_code == 202
    assert deployment.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_deployment_cancellation_is_safe_and_idempotent(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    project = (await client.post("/projects", json=PROJECT)).json()
    queued = (await client.post(f"/projects/{project['id']}/deploy")).json()

    cancelled = await client.post(f"/deployments/{queued['id']}/cancel")
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_requested"] is True
    repeated = await client.post(f"/deployments/{queued['id']}/cancel")
    assert repeated.status_code == 202
    assert repeated.json()["status"] == "cancelled"

    building = (await client.post(f"/projects/{project['id']}/deploy")).json()
    async with session_factory() as session:
        stored = await session.get(Deployment, uuid.UUID(building["id"]))
        assert stored is not None
        stored.status = DeploymentStatus.BUILDING
        await session.commit()

    requested = await client.post(f"/deployments/{building['id']}/cancel")
    assert requested.status_code == 202
    assert requested.json()["status"] == "building"
    assert requested.json()["cancel_requested"] is True

    async with session_factory() as session:
        stored = await session.get(Deployment, uuid.UUID(building["id"]))
        assert stored is not None
        stored.status = DeploymentStatus.RUNNING
        stored.active_token = None
        stored.cancel_requested = False
        await session.commit()
    assert (await client.post(f"/deployments/{building['id']}/cancel")).status_code == 409


@pytest.mark.asyncio
async def test_delete_is_queued_after_deploy_finishes(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    project = (await client.post("/projects", json=PROJECT)).json()
    deployment = (await client.post(f"/projects/{project['id']}/deploy")).json()
    blocked = await client.delete(f"/projects/{project['id']}")
    assert blocked.status_code == 409

    async with session_factory() as session:
        stored = await session.get(Deployment, uuid.UUID(deployment["id"]))
        assert stored is not None
        stored.status = DeploymentStatus.RUNNING
        stored.active_token = None
        await session.commit()

    deleted = await client.delete(f"/projects/{project['id']}")
    assert deleted.status_code == 202
    async with session_factory() as session:
        stored_project = await session.get(Project, uuid.UUID(project["id"]))
        assert stored_project is not None
        assert stored_project.state is ProjectState.DELETING


@pytest.mark.asyncio
async def test_stop_start_and_rollback_are_queued_with_state_guards(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    project = (await client.post("/projects", json=PROJECT)).json()
    project_id = uuid.UUID(project["id"])
    running_id = uuid.uuid4()
    target_id = uuid.uuid4()
    async with session_factory() as session:
        session.add_all(
            [
                Deployment(
                    id=running_id,
                    project_id=project_id,
                    task_id="running-task",
                    status=DeploymentStatus.RUNNING,
                    container_id="running-container",
                    image_tag="deployforge/example-api:new",
                ),
                Deployment(
                    id=target_id,
                    project_id=project_id,
                    task_id="target-task",
                    status=DeploymentStatus.STOPPED,
                    container_id="target-container",
                    image_tag="deployforge/example-api:old",
                ),
            ]
        )
        await session.commit()

    stopped = await client.post(f"/projects/{project['id']}/stop")
    assert stopped.status_code == 202
    assert stopped.json()["lifecycle_state"] == "stopping"
    assert (await client.post(f"/projects/{project['id']}/stop")).status_code == 409

    async with session_factory() as session:
        stored_project = await session.get(Project, project_id)
        running = await session.get(Deployment, running_id)
        assert stored_project is not None and running is not None
        stored_project.lifecycle_state = LifecycleState.STOPPED
        running.status = DeploymentStatus.STOPPED
        await session.commit()

    started = await client.post(f"/projects/{project['id']}/start")
    assert started.status_code == 202
    assert started.json()["lifecycle_state"] == "starting"

    async with session_factory() as session:
        stored_project = await session.get(Project, project_id)
        running = await session.get(Deployment, running_id)
        target = await session.get(Deployment, target_id)
        assert stored_project is not None and running is not None and target is not None
        stored_project.lifecycle_state = LifecycleState.ACTIVE
        running.status = DeploymentStatus.RUNNING
        target.status = DeploymentStatus.STOPPED
        await session.commit()

    rollback = await client.post(
        f"/projects/{project['id']}/rollback",
        json={"deployment_id": str(target_id)},
    )
    assert rollback.status_code == 202
    assert rollback.json()["lifecycle_state"] == "rolling_back"

    async with session_factory() as session:
        stored_project = await session.get(Project, project_id)
        running = await session.get(Deployment, running_id)
        target = await session.get(Deployment, target_id)
        assert stored_project is not None and running is not None and target is not None
        stored_project.lifecycle_state = LifecycleState.ACTIVE
        running.status = DeploymentStatus.STOPPED
        target.status = DeploymentStatus.RUNNING
        await session.commit()

    unavailable = await client.post(
        f"/projects/{project['id']}/rollback",
        json={"deployment_id": str(uuid.uuid4())},
    )
    assert unavailable.status_code == 409
    fetched = await client.get(f"/projects/{project['id']}")
    assert fetched.json()["latest_status"] == "running"


@pytest.mark.asyncio
async def test_missing_resources_return_404(client: AsyncClient) -> None:
    identifier = uuid.uuid4()
    assert (await client.get(f"/projects/{identifier}")).status_code == 404
    assert (await client.get(f"/deployments/{identifier}")).status_code == 404
