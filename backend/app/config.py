from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://clipforge:clipforge@db:5432/clipforge"
    redis_url: str = "redis://redis:6379/0"
    encryption_key: str = ""
    jwt_secret: str = ""
    storage_dir: str = "/storage"
    max_vod_minutes: int = 240
    max_clips_per_job: int = 8
    openai_api_key: str = ""
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [part.strip() for part in self.cors_origins.split(",") if part.strip()]
        if not origins or "*" in origins:
            raise RuntimeError("CORS_ORIGINS must be an explicit allow-list, never '*'.")
        return origins


settings = Settings()
