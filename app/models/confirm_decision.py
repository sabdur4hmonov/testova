"""Teacher To'g'ri/Xato decision on a confirmed wrong WRITTEN answer.

Append-only log — NEVER read by grading. It captures ground truth for
validating and, later, safely tuning the confirm-similarity threshold: whether
the teacher agreed the answer was wrong (Xato) or overrode it (To'g'ri, meaning
the bot's "wrong" was itself a misread), together with the similarity that
produced the ask. Over time this shows whether any near-threshold ask is a real
misread — the only evidence that could justify moving CONFIRM_SIMILARITY_THRESHOLD.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ConfirmDecision(Base):
    __tablename__ = "confirm_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)  # telegram_id
    flow: Mapped[str] = mapped_column(String(16), nullable=False)  # "manual" | "saved"
    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    student_answer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    correct_answer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Levenshtein similarity to the closest accepted answer that produced the ask.
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True  = teacher tapped To'g'ri → the answer was actually correct → the bot's
    #         "wrong" was a misread (the ask mattered).
    # False = teacher tapped Xato   → the bot was right to mark it wrong.
    teacher_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<ConfirmDecision q={self.question_number} "
            f"sim={self.similarity} teacher_correct={self.teacher_correct}>"
        )
