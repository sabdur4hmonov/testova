"""
Monthly reconciliation: our computed Gemini cost vs. Google's actual bill.

The admin pastes Google's billed total for a window; we snapshot our own
computed total for the same window and store the delta. History is kept so the
alerts phase can flag when Google's real spend exceeds our logged total by more
than a tolerance (the uninstrumented-spend guard).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


from app.database import Base


class BillingReconciliation(Base):
    __tablename__ = "billing_reconciliation"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Free-form window label the admin reconciled, e.g. "2026-08" or "Aug 1–22".
    period_label: Mapped[str] = mapped_column(String(64), nullable=False)
    google_total_usd: Mapped[float] = mapped_column(Float, nullable=False)
    # Our computed cost for the same window, snapshotted at reconcile time.
    our_computed_usd: Mapped[float] = mapped_column(Float, nullable=False)
    delta_usd: Mapped[float] = mapped_column(Float, nullable=False)      # google − ours
    delta_pct: Mapped[float] = mapped_column(Float, nullable=False)      # delta / ours * 100
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # admin tg_id
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return (f"<BillingReconciliation {self.period_label} "
                f"google=${self.google_total_usd:.4f} ours=${self.our_computed_usd:.4f}>")
