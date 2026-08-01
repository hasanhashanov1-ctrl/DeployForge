from celery import Celery

from app.config import get_settings
from app.logging import configure_logging

configure_logging()
settings = get_settings()

celery_app = Celery(
    "deployforge",
    broker=settings.redis_url,
    include=["app.tasks"],
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_soft_time_limit=840,
    task_time_limit=900,
    result_backend=None,
    beat_schedule={
        "collect-runtime-logs": {
            "task": "deployforge.collect_runtime_logs",
            "schedule": 5.0,
            "options": {"expires": 4},
        }
    },
    timezone="UTC",
)
