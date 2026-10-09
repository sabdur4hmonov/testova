"""One answer-sheet photo in a checking run — its read, its student, its grade.

CP-4. Every per-sheet fact used to live in the chat's single FSM record
(`sheet_answers`, `student_name`, `pending_variant`, `confirm_pending`, …),
and each sheet walked the chat through its own FSM sub-state. So a second
photo either matched no handler (dropped silently) or overwrote the first
sheet's data (aiogram loads FSM state before our media lock runs). Here each
photo owns a row; FSM keeps only the RUN.

`status` is the sheet's position in the flow. Transitions go through
app.services.sheet_checks.transition, a single conditional UPDATE, so two
inputs racing for the same sheet (a double tap, two typed names) cannot both
take it:

  reading        the Gemini read is in flight
  working        a handler owns the sheet right now (between two waits)
  unreadable     the read had no content at all            (terminal)
  incomplete     the read left questions out (CP-2)       (terminal)
  needs_name     waiting for the teacher to type the name
  needs_variant  waiting for the teacher to pick the variant (saved flow)
  needs_confirm  waiting for To'g'ri/Xato on `confirm_pending[0]`
  graded         reported and persisted                    (terminal)
  failed         the read raised, or the variant had no key (terminal)
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, false, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SheetCheck(Base):
    __tablename__ = "sheet_checks"
    __table_args__ = (
        Index("ix_sheet_checks_user_run_status", "user_id", "run_id", "status"),
        Index("ix_sheet_checks_user_prompt", "user_id", "prompt_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # telegram_id
    # One checking run (a project picked, or a key confirmed). Not an FK: a run
    # lives in FSM; this groups its sheets for the finish report.
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    flow: Mapped[str] = mapped_column(String(16), nullable=False)  # "saved" | "manual"
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    manual_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sheet_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # The read, kept so every later step works without a second Gemini call:
    # raw per-question values, allow_numeric, the variant candidate, blank /
    # unread, and (once typed) the classified answers/texts/unclear.
    read: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    student_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name_unclear: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    variant_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # CP-5: the edition the run is grading (variants are immutable per edition).
    edition_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Snapshot of the key this sheet is graded against.
    answer_key: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    open_questions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    confirm_pending: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    confirm_overrides: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # The bot message that asked this sheet's question (name / variant) — what a
    # reply binds to (CP-4 commit 2).
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    check_result_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<SheetCheck {self.flow} {self.status} run={self.run_id}>"
