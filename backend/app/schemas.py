from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class HealthResponse(BaseModel):
    ok: bool


class ErrorResponse(BaseModel):
    detail: str


class AuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: UUID
    email: str


class ApiKeyUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "groq", "together", "ollama", "compatible"]
    api_key: SecretStr = Field(min_length=1, max_length=512)
    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=200)


class ApiKeyUpsertResponse(BaseModel):
    configured: bool
    provider: str
    model: str


class ApiKeyMetadataResponse(BaseModel):
    configured: bool
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)


class JobCreateResponse(BaseModel):
    job_id: UUID


class JobListItemResponse(BaseModel):
    id: UUID
    source_url: str
    status: str
    progress_message: str
    error: str | None
    created_at: datetime


class ClipResponse(BaseModel):
    id: UUID
    start_s: float
    end_s: float
    score: int
    title: str
    hook: str
    score_source: str
    caption_status: str
    status: str
    error: str | None
    created_at: datetime


class JobDetailResponse(BaseModel):
    id: UUID
    source_url: str
    platform: str
    title: str | None
    duration_s: float | None
    thumbnail_url: str | None
    status: str
    progress_message: str
    error: str | None
    created_at: datetime
    updated_at: datetime
    clips: list[ClipResponse]
