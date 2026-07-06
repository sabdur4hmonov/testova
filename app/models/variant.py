from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Variant(Base):
    """
    A shuffled version of a project's questions.

    question_order: list of question IDs in the shuffled order for this variant.
        e.g. ["uuid-q3", "uuid-q1", "uuid-q2"]

    option_mapping: per-question option shuffle map.
        e.g. {"uuid-q1": {"A": "C", "B": "A", "C": "D", "D": "B"}}
        means original option A became C in this variant.

    answer_key: {question_number_in_variant: correct_option_in_variant}
        e.g. {"1": "B", "2": "D", "3": "A"}
    """

    __tablename__ = "variants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    variant_number: Mapped[int] = mapped_column(Integer, nullable=False)
    question_order: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    option_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    answer_key: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="variants")  # type: ignore[name-defined]
    submissions: Mapped[list["Submission"]] = relationship(  # type: ignore[name-defined]
        back_populates="variant", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<Variant #{self.variant_number} project={self.project_id}>"
