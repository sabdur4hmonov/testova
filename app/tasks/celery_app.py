from celery import Celery
from app.config import settings

celery_app = Celery(
    "testova",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.file_tasks",
        "app.tasks.notification_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.file_tasks.*": {"queue": "file_processing"},
        "app.tasks.notification_tasks.*": {"queue": "notifications"},
    },
    beat_schedule={
        "reset-daily-usage": {
            "task": "app.tasks.file_tasks.reset_daily_usage",
            "schedule": 3600.0,  # every hour — idempotent, checks date internally
        },
    },
)
