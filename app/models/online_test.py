"""Online tests taken in a browser (online/ service, handlers/online.py).

Deliberately NOT imported from app/models/__init__.py: importers of these
models (the online service, the bot's online handler, their tests) import this
module themselves. See docs/YANGI_FUNKSIYALAR.md.

Nothing here touches exam_editions / variants: an online test draws from a
saved Project's Question rows directly, per student, at start time.

Correct answers are never stored on these tables — grading reads the live
Question rows on the server — and nothing in them is ever sent to a browser
except a question's text and its shuffled option texts.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# online_tests.status
TEST_OPEN = "open"
TEST_CLOSED = "closed"

# online_attempts.status
ATTEMPT_ACTIVE = "active"
ATTEMPT_FINISHED = "finished"      # submitted by the student
ATTEMPT_EXPIRED = "expired"        # the total time ran out (graded as is)
ATTEMPT_CAUGHT = "caught"          # third violation: closed, score 0
ATTEMPT_ANNULLED = "annulled"      # teacher allowed a retake; kept for the record

# online_attempt_questions.status
AQ_ACTIVE = "active"
AQ_REPLACED = "replaced"           # swapped for a reserve question after a violation


class OnlineTest(Base):
    __tablename__ = "online_tests"
    __table_args__ = (UniqueConstraint("code", name="uq_online_tests_code"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False)        # N shown per student
    total_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_question_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_back: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Frozen at creation: the multiple-choice question ids this test draws from.
    pool_question_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Pool < 1.5 N: no replacement after a violation, only order is shuffled.
    replacement_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lang: Mapped[str] = mapped_column(String(4), nullable=False, default="uz")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TEST_OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    attempts: Mapped[list["OnlineAttempt"]] = relationship(
        back_populates="test", cascade="all, delete-orphan", lazy="select",
    )


class OnlineAttempt(Base):
    __tablename__ = "online_attempts"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_online_attempts_token_hash"),
        # One live attempt per student name per test; an annulled one does not
        # count, which is exactly what lets the teacher allow a retake.
        Index(
            "uq_online_attempts_live_name", "test_id", "name_key", unique=True,
            postgresql_where=text("status <> 'annulled'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    test_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("online_tests.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_name: Mapped[str] = mapped_column(String(64), nullable=False)
    class_name: Mapped[str] = mapped_column(String(32), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ATTEMPT_ACTIVE)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    violations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Start of a hidden/blur period not yet closed by a "visible" event.
    away_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    annulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    test: Mapped[OnlineTest] = relationship(back_populates="attempts")
    questions: Mapped[list["OnlineAttemptQuestion"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", lazy="select",
    )
    events: Mapped[list["OnlineEvent"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", lazy="select",
    )


class OnlineAttemptQuestion(Base):
    __tablename__ = "online_attempt_questions"
    __table_args__ = (
        Index(
            "uq_online_aq_live_position", "attempt_id", "position", unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("online_attempts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)   # 1..N as shown
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False,
    )
    # Original option letters in the order this student sees them.
    option_order: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Original letters the student picked (empty/NULL = unanswered).
    answer: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AQ_ACTIVE)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    attempt: Mapped[OnlineAttempt] = relationship(back_populates="questions")


class OnlineEvent(Base):
    """Every focus/visibility report and every decision taken on it, with the
    SERVER's clock. The teacher's violation log is read from here."""

    __tablename__ = "online_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("online_attempts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempt: Mapped[OnlineAttempt] = relationship(back_populates="events")
