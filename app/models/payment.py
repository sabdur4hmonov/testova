"""A payment EVENT: one row per time money actually arrived.

WHY A NEW TABLE, AND NOT `subscriptions`
----------------------------------------
`subscriptions` (migration 001) looks like the obvious home and is not, for
three independent reasons — each of which alone would rule it out:

1. IT IS DEAD. No code anywhere constructs a `Subscription`; the table holds 0
   rows on the dev DB. Reviving a table nothing writes is not "reusing" it.
2. IT IS A CURRENT-STATE RECORD, NOT A LEDGER. Its fields are `is_active` and
   `expires_at` — mutable "what is true now" state. But "what is true now" is
   ALREADY owned by `users` (monthly_variant_limit, monthly_check_limit,
   book_limit, access_until, price_som_at_purchase), and `plans.plan_for()`
   derives the tier from exactly those columns. Writing `subscriptions` would
   create a SECOND source of truth for "what plan is this user on" that could
   disagree with the first. This project has already been bitten twice by a
   segment being re-derived instead of read from its one definition (the
   dashboard's wrong trial count, commit b58f4ed). Not again.
3. ITS `plan` COLUMN IS THE WRONG TAXONOMY. It is typed
   `subscription_plan_enum` with values free/pro/center — a legacy tier set
   that predates today's standart/pro. Storing a current sale in it needs an
   ALTER TYPE on a live enum, for no gain.

What is genuinely MISSING from the schema is the thing migration 029 said out
loud: "nothing in the schema can date a purchase". `period_start` is rewritten
by BOTH set_plan and reset_quota, so a renewal makes an August sale
indistinguishable from a September one. That is the hole this table fills, and
an append-only event ledger is the shape that fills it.

THE RELATIONSHIP between the two, stated once:
  * `users`    — current state. What this teacher may do TODAY. Authoritative.
  * `payments` — history. What was collected, WHEN, and for which tier. Never
                 mutated, never consulted to decide access.
  * `subscriptions` — dead. Left untouched (dropping a table is not this
                 feature's business), see its own model docstring.

A payment NEVER gates access. Access is `users.access_until` and the limit
columns, exactly as before. Recording a payment can APPLY a plan (by calling
the existing admin_users.set_plan), but the ledger row is not consulted by any
quota or access check — so a mis-entered payment is a reporting error, never
an outage.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# How the money arrived. 'manual' is the ONLY value today — payment is manual
# by design (plans.py:4, user.py:71, and the bot tells teachers so). The column
# exists so that wiring a real gateway later is an INSERT with a different
# string, not a migration of every historical row.
PAYMENT_METHODS: tuple[str, ...] = ("manual",)


class Payment(Base):
    __tablename__ = "payments"

    # Read two ways, both covered:
    #   * (user_id, paid_at DESC) — the per-user payment history on the detail
    #     page, and "is this teacher late?" which needs only their LATEST row;
    #   * (paid_at) — the revenue overview's period totals.
    __table_args__ = (
        Index("ix_payments_user_id_paid_at", "user_id", "paid_at"),
        Index("ix_payments_paid_at", "paid_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # users.id (the UUID), not telegram_id — a financial row must not dangle,
    # and this is the same FK shape Book.user_id and Subscription.user_id use.
    # CASCADE matches them; no code path deletes a user today.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # plans.PLANS key ('standart' | 'pro'). A plain String, NOT an enum:
    # plans.py is the single source of truth for what tiers exist, a new tier
    # must not require an ALTER TYPE on a live database, and this codebase
    # already has one open enum defect. A guard test pins it to PLANS.keys().
    plan_key: Mapped[str] = mapped_column(String(16), nullable=False)

    # What was ACTUALLY COLLECTED, in so'm — deliberately NOT derived from
    # plans.PLANS[plan_key].price_som at read time. The list price is a live
    # constant that has already moved once (50k->75k, 100k->150k), discounts
    # and part-payments happen, and revenue must be what came in rather than
    # what the tier costs today. This is the same reasoning that put
    # users.price_som_at_purchase in migration 029, applied per event.
    amount_som: Mapped[int] = mapped_column(Integer, nullable=False)

    # When the MONEY arrived. Admin-settable and back-datable: the whole point
    # is logging a bank transfer that happened before anyone opened the panel.
    # Distinct from created_at (when the ROW was typed in) — both are kept, so
    # a back-dated entry is visible as one.
    paid_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Coverage bought, in days. Stored per row rather than read from
    # plans.PLAN_DAYS at query time, because "is this teacher late this month?"
    # is answered as paid_at + period_days < now. Without it, a 60-day
    # prepayment would be scored late after 30, and a future change to
    # PLAN_DAYS would silently rewrite the meaning of every historical row.
    period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    method: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    # Bank transfer id / Payme receipt / "naqd" — free text, operator's choice.
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Telegram id of the admin who recorded it (admin_log carries the same
    # event; this denormalises the actor so the ledger reads standalone).
    recorded_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="payments")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Payment {self.amount_som} som {self.plan_key} user={self.user_id}>"
