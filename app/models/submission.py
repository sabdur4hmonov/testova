from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Submission(Base):
    """Student answer sheet submission and grading result."""

    __tablename__ = "submissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("variants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    student_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    answer_sheet_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Raw extracted answers from the answer sheet image
    # e.g. {"1": "A", "2": "C", "3": null}  (null = no answer detected)
    student_answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Grading results per question
    # e.g. {"1": {"student": "A", "correct": "A", "is_correct": true}, ...}
    results: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    correct_count: Mapped[int | None] = mapped_column(nullable=True)
    wrong_count: Mapped[int | None] = mapped_column(nullable=True)
    skipped_count: Mapped[int | None] = mapped_column(nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    variant: Mapped["Variant"] = relationship(back_populates="submissions")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Submission id={self.id} score={self.score}>"
