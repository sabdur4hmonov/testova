from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DDL, DateTime, ForeignKey, Integer, String, UniqueConstraint, event, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Variant(Base):
    """
    A shuffled version of a project's questions, belonging to ONE edition.

    question_order: list of question IDs in the shuffled order for this variant.
        e.g. ["uuid-q3", "uuid-q1", "uuid-q2"]

    option_mapping: per-question option shuffle map.
        e.g. {"uuid-q1": {"A": "C", "B": "A", "C": "D", "D": "B"}}
        means original option A became C in this variant.

    answer_key: {question_number_in_variant: correct_option_in_variant}
        e.g. {"1": "B", "2": "D", "3": "A"}

    open_positions: printed positions that are open (written) questions, frozen
        at generation (CP-5). NULL for variants created before migration 033 —
        grading then derives them from the live Question rows.

    IMMUTABLE (CP-5). A delivered paper is graded against this row, so its
    grading columns are never rewritten: regenerating creates a new
    ExamEdition with new rows (app.services.variant_store.create_edition), and
    the `variants_frozen` trigger refuses an UPDATE that tries.
    """

    __tablename__ = "variants"
    # AUDIT B-17 / CP-5. A variant number is unique within its EDITION: a
    # project's second edition prints "Variant 1" again, with its own key.
    __table_args__ = (
        UniqueConstraint("edition_id", "variant_number",
                         name="uq_variants_edition_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("exam_editions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    variant_number: Mapped[int] = mapped_column(Integer, nullable=False)
    question_order: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    option_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    answer_key: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    open_positions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="variants")  # type: ignore[name-defined]
    edition: Mapped["ExamEdition"] = relationship(back_populates="variants")  # type: ignore[name-defined]
    submissions: Mapped[list["Submission"]] = relationship(  # type: ignore[name-defined]
        back_populates="variant", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<Variant #{self.variant_number} project={self.project_id}>"


# The trigger is part of the schema, not only of migration 033: the test suite
# builds its databases with create_all, and they must refuse a rewrite too.
# Same SQL as migrations/versions/033_exam_editions.py.
VARIANTS_FROZEN_FUNCTION = """
CREATE OR REPLACE FUNCTION variants_frozen() RETURNS trigger AS $$
BEGIN
    IF NEW.answer_key IS DISTINCT FROM OLD.answer_key
       OR NEW.question_order IS DISTINCT FROM OLD.question_order
       OR NEW.option_mapping IS DISTINCT FROM OLD.option_mapping
       OR NEW.variant_number IS DISTINCT FROM OLD.variant_number
       OR NEW.open_positions IS DISTINCT FROM OLD.open_positions
       OR NEW.edition_id IS DISTINCT FROM OLD.edition_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id THEN
        RAISE EXCEPTION 'variants are immutable - a delivered paper is graded against this row'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""
VARIANTS_FROZEN_TRIGGER = (
    "CREATE TRIGGER variants_frozen BEFORE UPDATE ON variants "
    "FOR EACH ROW EXECUTE FUNCTION variants_frozen()"
)

event.listen(Variant.__table__, "after_create", DDL(VARIANTS_FROZEN_FUNCTION))
event.listen(Variant.__table__, "after_create", DDL(VARIANTS_FROZEN_TRIGGER))
