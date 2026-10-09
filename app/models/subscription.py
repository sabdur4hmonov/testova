"""DEAD TABLE — mapped, migrated, and written by nothing.

No code in this repo constructs a `Subscription`; the table holds 0 rows. It
survives from migration 001, when the plan was to keep subscription state in
its own table. That is not how this codebase works now: a user's plan is
DERIVED from their limit columns on `users` (see app/services/plans.py), and
`plans.plan_for()` is the one definition.

It is deliberately NOT the home of the payment ledger — see
app/models/payment.py for why (dead, wrong shape, legacy free/pro/center enum).
Left in place rather than dropped: removing a table is a separate decision from
adding one, and it costs nothing standing empty.

DO NOT start writing this table without first resolving which of it and `users`
is authoritative for a user's plan. Two answers to that question is how the
dashboard's trial count went wrong (commit b58f4ed).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import SubscriptionPlan


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan: Mapped[SubscriptionPlan] = mapped_column(
        Enum(SubscriptionPlan, name="subscription_plan_enum"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(nullable=True)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="subscriptions")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Subscription plan={self.plan} user={self.user_id} active={self.is_active}>"
