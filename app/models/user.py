from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Language(str, enum.Enum):
    UZ = "uz"
    EN = "en"
    RU = "ru"


class SubscriptionPlan(str, enum.Enum):
    FREE = "free"
    PRO = "pro"
    CENTER = "center"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    language: Mapped[Language] = mapped_column(
        Enum(Language, name="language_enum", values_callable=lambda obj: [e.value for e in obj]),
        default=Language.UZ,
        nullable=False,
    )
    subscription_plan: Mapped[SubscriptionPlan] = mapped_column(
        Enum(SubscriptionPlan, name="subscription_plan_enum", values_callable=lambda obj: [e.value for e in obj]),
        default=SubscriptionPlan.FREE,
        nullable=False,
    )
    daily_projects_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_projects_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_projects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reset_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_banned: Mapped[bool] = mapped_column(default=False, nullable=False)  # legacy

    # ── Access control ─────────────────────────────────────────────────────────
    # NULL on a dimension = unlimited on that dimension. Existing rows migrate
    # to NULL/NULL (unlimited) so nobody is locked out on deploy.
    access_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uses_left: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set on the user's FIRST-EVER charged use (atomic, once). Drives the admin
    # trial-conversion notification. NULL = has never charged.
    first_charged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # What this teacher actually PAID, in so'm, at their last sale. Written by
    # admin_users.set_plan (the sale) and by nothing else — renewal deliberately
    # leaves it alone, so a grandfathered price holds until an explicit re-sale.
    #
    # NOT a charging field: payment is manual and no code path moves money. It
    # exists because plans.price_som is a live constant, so raising a plan's
    # price silently re-priced every existing subscriber's account page.
    #
    # NULL = sold before this column existed. The read falls back to the live
    # constant; nothing can date an older purchase (period_start is rewritten by
    # every renewal), so a guess would be worse than the honest fallback.
    price_som_at_purchase: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Independent monthly quotas (rolling 30 days) ─────────────────────────
    # Separate from uses_left. NULL limit = unlimited on that dimension. Both
    # counters share one rolling window anchored at period_start.
    monthly_variant_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variant_count_this_period: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    monthly_check_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    check_count_this_period: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Bepul's answer-sheet checks are LIFETIME, not monthly. They cannot use
    # check_count_this_period, because _apply_rolling_reset zeroes every period
    # counter off the one shared period_start — so a trial capped at 2 would
    # silently refill every 30 days. This counter is touched by NO reset; which
    # of the two a user's CHECK quota reads is decided by plans.is_trial.
    #
    # Why not reuse uses_left the way trial GENERATION does: uses_left is a
    # single counter already spent by the upload path, and checking must stay
    # independent of generation (can_check deliberately ignores uses_left, so a
    # teacher out of generations can still grade what they made).
    check_count_lifetime: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Textbook library quotas (TWO separate meters, deliberately) ──────────
    # 1. book_limit — a TOTAL cap on how many books the teacher OWNS. It does
    #    NOT reset: a library is permanent, so a monthly reset would either
    #    force deletions or let the library grow without bound. Compared against
    #    a live COUNT(*) of the user's books, so there is no counter to drift.
    #    Named without the `monthly_` prefix precisely because it never rolls
    #    over — the other limits on this model do. NULL = unlimited.
    book_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 2. Book ANALYSIS is the recurring COST (a paid Gemini pass over a new
    #    book), so it gets its own rolling-monthly meter, sharing period_start
    #    with the variant/check counters. Completely independent of them: an
    #    exhausted variant quota must never block adding a book, or vice versa.
    monthly_book_analysis_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    book_analysis_count_this_period: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # 3. Book GENERATION — RETIRED as a separate meter. Book generation and
    #    variant generation now draw from ONE shared pool (20 Standart / 40
    #    Pro), which is monthly_variant_limit above; quota.TEST maps to it and
    #    both paths consume it. These two columns are kept, unset and unread,
    #    because dropping them is not additive — see migration 028. Do not
    #    revive them without first deciding what a second book budget means
    #    next to the shared one.
    monthly_book_generation_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    book_generation_count_this_period: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    projects: Mapped[list["Project"]] = relationship(back_populates="user", lazy="select")  # type: ignore[name-defined]
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", lazy="select")  # type: ignore[name-defined]
    payments: Mapped[list["Payment"]] = relationship(back_populates="user", lazy="select")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<User tg_id={self.telegram_id} name={self.full_name!r}>"
