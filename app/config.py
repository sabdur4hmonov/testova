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
    # Sized for many concurrent graders. pool_size + max_overflow = 75 peak
    # connections — Postgres's own max_connections (default 100) must cover this
    # plus any other clients, or checkouts will fail under load.
    DATABASE_POOL_SIZE: int = 25
    DATABASE_MAX_OVERFLOW: int = 50
    DATABASE_ECHO: bool = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── AI ────────────────────────────────────────────────────────────────────
    GEMINI_API_KEY: str = ""
    # gemini-2.0-flash-exp was shut down 2026-06-01; keep the fallback valid.
    # (.env overrides this; live behavior is unchanged.)
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_MAX_RETRIES: int = 3
    # Grading-only budget (answer-sheet reads). SEPARATE from GEMINI_MAX_RETRIES,
    # which governs question extraction/generation and must stay generous. A
    # teacher waiting on a graded photo needs a tight worst case, not 4.5 min.
    # TOTAL attempts (1 initial + up to 2 retries) — retried on timeout/error AND
    # on an empty/truncated read: 3 x 15s + 2 x 1s backoff ≈ 47s worst case.
    GEMINI_GRADING_MAX_RETRIES: int = 3
    # Per-attempt seconds. MEASURED (thinking disabled, real sheets, n=10):
    # p50 7.4s, p90 11.1s, max 12.3s — so 25s is >2x p90 headroom. The old 15s
    # was too tight: with thinking ON a 25-question sheet took ~26s, so it
    # ALWAYS timed out, retried, and could end up empty — surfacing as a FALSE
    # "Rasm aniq chiqmagan" on a perfectly readable photo. Abandoning a slow but
    # valid call is worse than waiting: the teacher gets a bogus retake request.
    GEMINI_GRADING_TIMEOUT: int = 25
    # Cap simultaneous grading Gemini calls so a burst of photos can't blow past
    # Gemini's rate limits. Mirrors ai_analyzer's MAX_CONCURRENT (extraction).
    MAX_CONCURRENT_GRADING: int = 6
    # Cost tracking (read-only). Prices are USD per 1M tokens; so'm via UZS rate.
    GEMINI_PRICE_IN_PER_M: float = 0.30    # gemini-2.5-flash input list price
    GEMINI_PRICE_OUT_PER_M: float = 2.50   # gemini-2.5-flash output list price
    UZS_PER_USD: float = 12000.0

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

    # ── Exam timer ────────────────────────────────────────────────────────────
    # Wall-clock times a teacher types ("14:30") are interpreted in this fixed
    # offset. Uzbekistan is UTC+5 year-round (no DST), so a fixed offset is
    # exact and needs no tzdata/zoneinfo dependency.
    EXAM_TZ_OFFSET_HOURS: int = 5

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
