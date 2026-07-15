"""Graded answer-sheet result (one row per checked sheet)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CheckResult(Base):
    """
    One graded answer sheet. Works for BOTH grading paths:
      * manual "Javob orqali" flow → manual_session_id set, project_id NULL.
      * (future) saved-project flow → project_id set, manual_session_id NULL.
    """

    __tablename__ = "check_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)  # telegram_id
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    manual_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    variant_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Reserved for a later phase (naming graded students); included now so we
    # never have to migrate this table twice.
    student_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wrong_answers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    unclear: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<CheckResult {self.score}/{self.total} variant={self.variant_number}>"
