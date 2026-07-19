from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Answer options — LEGACY 4 fixed Latin slots. Kept for old rows + fallback;
    # new rows write `options` (below) instead and leave these NULL.
    option_a: Mapped[str | None] = mapped_column(Text, nullable=True)
    option_b: Mapped[str | None] = mapped_column(Text, nullable=True)
    option_c: Mapped[str | None] = mapped_column(Text, nullable=True)
    option_d: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Real, label-preserving options (migration 007): an ORDERED list of
    # {"letter": <as printed>, "text": <as written>}. Preserves the paper's
    # actual labels — Latin OR Cyrillic, any gaps (a,b,d,e), any count. NULL for
    # rows created before 007 (read via `options_ordered` fallback).
    options: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # LEGACY single-letter answer. Kept for old rows + fallback; new writes use
    # `correct_answers` (below).
    correct_answer: Mapped[str | None] = mapped_column(
        String(4), nullable=True
    )  # a single option LABEL, e.g. "A" | "D" | "Д" (Latin or Cyrillic; 1 char)

    # Accepted answers (migration 008): a LIST of accepted strings. A letter is a
    # one-item list ["A"]; a written multi-accept is ["PHONE","TELEPHONE"]. NULL
    # for rows created before 008 (read via `correct_answers_ordered` fallback).
    correct_answers: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Image support
    has_image: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    image_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Reading-comprehension group support
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Source location
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="questions")  # type: ignore[name-defined]

    @property
    def options_ordered(self) -> list[dict]:
        """Real options as an ORDERED list of {"letter","text"}.

        Prefers the label-preserving `options` JSON (new rows). Falls back to the
        legacy option_a..d columns for rows created before migration 007 — so old
        rows read exactly as before and nothing needs backfilling. Blank entries
        are dropped; labels are preserved verbatim (never folded here).
        """
        if self.options:
            return [
                {"letter": str(o["letter"]), "text": o["text"]}
                for o in self.options
                if isinstance(o, dict) and o.get("letter") and o.get("text")
            ]
        legacy = [("A", self.option_a), ("B", self.option_b),
                  ("C", self.option_c), ("D", self.option_d)]
        return [{"letter": L, "text": v} for L, v in legacy if v and str(v).strip()]

    @property
    def correct_answers_ordered(self) -> list[str]:
        """Accepted answers as a list. Prefers the JSON `correct_answers` (new
        rows); falls back to the legacy single-letter `correct_answer` for old
        rows (so they grade unchanged). Empty list = no answer set."""
        if self.correct_answers:
            return [str(a) for a in self.correct_answers if a is not None and str(a).strip()]
        if self.correct_answer and str(self.correct_answer).strip():
            return [str(self.correct_answer)]
        return []

    @property
    def options_dict(self) -> dict:
        """Ordered {label: text} view of `options_ordered` (Python dicts keep
        insertion order, so display/shuffle order is preserved)."""
        return {o["letter"]: o["text"] for o in self.options_ordered}

    def to_dict(self) -> dict:
        return {
            "question_id": str(self.id),
            "question_number": self.question_number,
            "question_text": self.question_text,
            "options": self.options_dict,
            "correct_answer": self.correct_answer,
            "correct_answers": self.correct_answers_ordered,
            "has_image": self.has_image,
            "image_path": self.image_path,
        }

    def __repr__(self) -> str:
        return f"<Question #{self.question_number} project={self.project_id}>"
