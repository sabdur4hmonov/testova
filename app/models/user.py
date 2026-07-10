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
        Enum(Language, name="language_enum"), default=Language.UZ, nullable=False
    )
    subscription_plan: Mapped[SubscriptionPlan] = mapped_column(
        Enum(SubscriptionPlan, name="subscription_plan_enum"),
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

    def __repr__(self) -> str:
        return f"<User tg_id={self.telegram_id} name={self.full_name!r}>"
