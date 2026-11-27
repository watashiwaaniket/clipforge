from celery import Celery
from celery.signals import worker_process_init

from app.config import settings
from app.db import rebind_worker_engine
from app.logging import configure_logging
from app.security import validate_runtime_secrets

configure_logging()

celery_app = Celery("clipforge", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_ignore_result=True,
    worker_hijack_root_logger=False,
    worker_concurrency=1,
    worker_max_tasks_per_child=1,
    worker_max_memory_per_child=2_500_000,
    task_soft_time_limit=settings.job_timeout_seconds,
    task_time_limit=settings.job_timeout_seconds + 60,
    broker_transport_options={"visibility_timeout": 7200},
    task_routes={
        "process_job": {"queue": "celery"},
        "reap_stale_jobs": {"queue": "reaper"},
    },
    beat_schedule={
        "reap-stale-jobs": {
            "task": "reap_stale_jobs",
            "schedule": 60.0,
        },
    },
    imports=["app.workers.pipeline"],
)


@worker_process_init.connect
def on_worker_process_init(**_kwargs: object) -> None:
    from app.workers.pipeline import init_worker_instance_id

    init_worker_instance_id()
    validate_runtime_secrets()
    rebind_worker_engine()
