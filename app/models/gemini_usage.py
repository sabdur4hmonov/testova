"""Read-only accounting of Gemini API token usage (one row per API call)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GeminiUsage(Base):
    __tablename__ = "gemini_usage"

    # (user_id, created_at) serves the per-user cost reads (admin cost pages,
    # usage_summary, the book-generation ceiling). user_id leads because those
    # all filter it for equality and then bound/order by created_at. The
    # standalone created_at index below stays for the global time-window
    # aggregates, which have no user_id predicate. See migration 025.
    __table_args__ = (
        Index("ix_gemini_usage_user_id_created_at", "user_id", "created_at"),
        Index("ix_gemini_usage_call_site_created_at", "call_site", "created_at"),
        # (kind, user_id) serves the book-usage page's analysis tally, which
        # filters kind with IN and then groups by user_id — the (user_id,
        # created_at) index above cannot help it, since user_id carries no
        # equality predicate there. See migration 031.
        Index("ix_gemini_usage_kind_user_id", "kind", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # telegram_id
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="extract")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thinking_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Prompt tokens served from Gemini's implicit prefix cache. Billed at a
    # reduced rate, and the ONLY way to tell whether caching is hitting at all.
    cached_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Finer than `kind`: which code path made the call (e.g. "extract.page" vs
    # "extract.scheme" — both log kind="extract"). NULL on historical rows.
    call_site: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<GeminiUsage {self.kind} model={self.model} total={self.total_tokens}>"
