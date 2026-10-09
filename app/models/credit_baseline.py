"""
Google/Gemini credit-balance baseline (admin panel).

Google exposes no real-time balance API, so the admin enters their current
credit once and the panel computes `remaining = credit_usd − Σ(our computed
cost for usage AFTER as_of)`. Each row is an append-only baseline snapshot; the
CURRENT baseline is the most recent row. When a reconciliation against Google's
real bill reveals drift, the admin sets a NEW baseline (a new row) rather than
editing history — so the balance can be re-anchored to truth without losing the
audit trail of when/why it was adjusted.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CreditBaseline(Base):
    __tablename__ = "credit_baseline"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Credit available AS OF `as_of` (USD). Remaining = this − cost since as_of.
    credit_usd: Mapped[float] = mapped_column(Float, nullable=False)
    # Usage with created_at >= as_of counts against this baseline. Defaults to
    # the moment the baseline is set, so prior historical spend is excluded.
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # admin tg_id
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<CreditBaseline ${self.credit_usd:.2f} as_of={self.as_of:%Y-%m-%d}>"
