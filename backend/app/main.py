import logging
import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


configure_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("api_start")
    yield


app = FastAPI(title="ClipForge", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/api/v1/health")
def health() -> dict[str, bool]:
    return {"ok": True}
