"""Audit log of admin access-control actions."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Every `action` string this codebase writes, alphabetical (the order the
# removed `SELECT DISTINCT action ORDER BY action` produced, so the audit
# page's dropdown is unchanged).
#
# AUDIT C-26: that DISTINCT ran on EVERY audit page render — a full scan of an
# append-only table, to rebuild a list that is fixed at compile time. The set
# is small and enumerable, so it is enumerated here instead. Sourced by
# reading all four write sites, NOT by querying the dev database, which only
# happened to contain 8 of these:
#   * app/services/admin_users.py:36  _write_log(...)  — 15 of them
#   * app/admin/web/services/moderation.py:50          — book_delete
#   * app/services/broadcast.py:109, :122              — message, broadcast
#   * app/admin/web/services/revenue.py                — payment
#
# A new action added to any of those sites MUST be added here or it silently
# vanishes from the filter dropdown. tests/test_admin_panel_services.py has a
# drift guard that scans those sources and fails if this list falls behind.
ADMIN_LOG_ACTIONS: tuple[str, ...] = (
    "addcheck",
    "addvariant",
    "book_delete",
    "broadcast",
    "extend",
    "grant",
    "message",
    "payment",
    "plan",
    "resetquota",
    "revoke",
    "setbookanalysislimit",
    "setbookgenerationlimit",
    "setbooklimit",
    "setchecklimit",
    "setprice",
    "setuses",
    "setvariantlimit",
    "unblock",
)


class AdminLog(Base):
    __tablename__ = "admin_log"

    # The audit page reads this table two ways, and neither one filters
    # admin_id (the only column that used to be indexed):
    #   * (created_at) — the unfiltered listing and the CSV export, both
    #     ORDER BY created_at DESC;
    #   * (action, created_at) — the same listing narrowed by the dropdown
    #     filter, where equality on `action` leads and `created_at` then
    #     satisfies the sort without a re-sort.
    # See migration 030.
    __table_args__ = (
        Index("ix_admin_log_created_at", "created_at"),
        Index("ix_admin_log_action_created_at", "action", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # target telegram_id
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<AdminLog {self.action} by={self.admin_id} target={self.target}>"
