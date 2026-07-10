from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Bot ───────────────────────────────────────────────────────────────────
    BOT_TOKEN: str
    WEBHOOK_URL: Optional[str] = None
    WEBHOOK_PATH: str = "/webhook"
    WEBHOOK_SECRET: Optional[str] = None

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://testova:password@localhost:5432/testova_db"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_ECHO: bool = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── AI ────────────────────────────────────────────────────────────────────
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash-exp"
    GEMINI_MAX_RETRIES: int = 3

    # ── Storage ───────────────────────────────────────────────────────────────
    STORAGE_TYPE: str = "local"  # "local" | "s3"
    LOCAL_STORAGE_PATH: str = "./storage"
    S3_BUCKET: Optional[str] = None
    S3_ACCESS_KEY: Optional[str] = None
    S3_SECRET_KEY: Optional[str] = None
    S3_ENDPOINT_URL: Optional[str] = None
    S3_REGION: str = "us-east-1"

    # ── App ───────────────────────────────────────────────────────────────────
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    MAX_FILE_SIZE_MB: int = 50
    ADMIN_IDS: list[int] = Field(default_factory=list)

    # ── Access control ────────────────────────────────────────────────────────
    TRIAL_DAYS: int = 30          # free trial window applied on first /start
    TRIAL_USES: int = 1           # free full cycles for a fresh user
    ADMIN_USERNAME: str = "admin"  # shown in the blocked message (no leading @)

    # ── Subscription limits ───────────────────────────────────────────────────
    FREE_DAILY_PROJECTS: int = 3
    PRO_DAILY_PROJECTS: int = 50
    CENTER_DAILY_PROJECTS: int = 500

    # ── Admin API ─────────────────────────────────────────────────────────────
    ADMIN_API_SECRET: str = "change_me_in_production"

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def max_file_size_bytes(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def is_webhook_mode(self) -> bool:
        return bool(self.WEBHOOK_URL)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
