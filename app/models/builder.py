"""Multi-Source Test Builder session models.

A session collects several uploaded test files (each processed through the
normal pipeline into its own Project + Question rows); the pool is simply
the union of the session's projects' questions, keyed by Question.id — so
original-numbering collisions between files can never corrupt answers (P6).
Sessions live in the DB (P1: bot restarts don't lose the pool) and expire
lazily 48h after creation.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

SESSION_TTL_HOURS = 48


class BuilderStatus(str, enum.Enum):
    ACTIVE = "active"
    FINISHED = "finished"
    SAVED = "saved"
    CANCELLED = "cancelled"


class BuilderSession(Base):
    __tablename__ = "builder_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    status: Mapped[BuilderStatus] = mapped_column(
        Enum(BuilderStatus, name="builder_status_enum"),
        default=BuilderStatus.ACTIVE, nullable=False, index=True,
    )
    # Project owning the generated pool variants (created at finish time)
    pool_project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    sources: Mapped[list["BuilderSource"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<BuilderSession {self.id} user={self.user_id} {self.status}>"


class BuilderSource(Base):
    __tablename__ = "builder_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("builder_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_question_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warnings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    key_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped["BuilderSession"] = relationship(back_populates="sources")

    def __repr__(self) -> str:
        return f"<BuilderSource {self.filename} q={self.question_count}>"


def default_expiry(now: datetime) -> datetime:
    return now + timedelta(hours=SESSION_TTL_HOURS)


def is_expired(session_expires_at: datetime | None, now: datetime) -> bool:
    """Lazy expiry decision (P1) — pure so it's testable offline."""
    return session_expires_at is not None and now >= session_expires_at
