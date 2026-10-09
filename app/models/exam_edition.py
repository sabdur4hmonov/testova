"""One printed edition of a test (CP-5)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ExamEdition(Base):
    """One delivery of a project's variants: one generate -> PDFs -> send.

    AUDIT B-17 / CP-5. Variants used to be upserted in place, so regenerating a
    project rewrote the keys of papers already delivered. An edition is never
    rewritten: a regeneration creates the NEXT edition with its own variant
    rows, and the `variants_frozen` trigger refuses any UPDATE of a stored key.

    `code` is printed next to "Variant N" and on the answer key, so a paper can
    be matched to the edition it came from. `option_labels` is the snapshot of
    the labels this edition offers (NULL for legacy editions: read live).
    """

    __tablename__ = "exam_editions"
    __table_args__ = (
        UniqueConstraint("project_id", "edition_number", name="uq_exam_editions_project_number"),
        UniqueConstraint("code", name="uq_exam_editions_code"),
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
    edition_number: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(8), nullable=False)
    # upload | book | multi | legacy (backfilled by migration 033)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    variant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pdf_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    option_labels: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="editions")  # type: ignore[name-defined]
    variants: Mapped[list["Variant"]] = relationship(  # type: ignore[name-defined]
        back_populates="edition", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<ExamEdition #{self.edition_number} {self.code} project={self.project_id}>"
