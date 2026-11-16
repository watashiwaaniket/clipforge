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
)


@worker_process_init.connect
def on_worker_process_init(**_kwargs: object) -> None:
    validate_runtime_secrets()
    rebind_worker_engine()
