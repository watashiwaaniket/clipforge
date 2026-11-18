from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.clips import router as clips_router
from app.api.jobs import router as jobs_router
from app.api.keys import router as keys_router
from app.api.providers import router as providers_router
from app.config import settings
from app.logging import configure_logging
from app.security import validate_runtime_secrets

configure_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_runtime_secrets()
    log.info("api_start")
    yield


app = FastAPI(title="ClipForge", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth_router)
app.include_router(keys_router)
app.include_router(providers_router)
app.include_router(jobs_router)
app.include_router(clips_router)


@app.get("/api/v1/health")
def health() -> dict[str, bool]:
    return {"ok": True}
