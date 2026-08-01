import uuid

from app.celery_app import celery_app


def enqueue_deployment(deployment_id: uuid.UUID, task_id: str) -> None:
    celery_app.send_task(
        "deployforge.deploy_project",
        args=[str(deployment_id)],
        task_id=task_id,
    )


def enqueue_project_cleanup(project_id: uuid.UUID) -> None:
    celery_app.send_task(
        "deployforge.cleanup_project",
        args=[str(project_id)],
        task_id=f"cleanup-{project_id}",
    )


def enqueue_project_stop(project_id: uuid.UUID) -> None:
    celery_app.send_task(
        "deployforge.stop_project",
        args=[str(project_id)],
        task_id=f"stop-{project_id}",
    )


def enqueue_project_start(project_id: uuid.UUID) -> None:
    celery_app.send_task(
        "deployforge.start_project",
        args=[str(project_id)],
        task_id=f"start-{project_id}",
    )


def enqueue_project_rollback(project_id: uuid.UUID, deployment_id: uuid.UUID) -> None:
    celery_app.send_task(
        "deployforge.rollback_project",
        args=[str(project_id), str(deployment_id)],
        task_id=f"rollback-{project_id}-{deployment_id}",
    )
