"""Index gemini_usage(user_id, created_at) — the per-user cost lookups.

AUDIT C-9. gemini_usage carries one row per Gemini API call, so it is the
fastest-growing table in the schema, and it was indexed on created_at ONLY.
Every per-user cost read therefore fell back to a sequential scan:

  * app/admin/web/services/costs.py:151, :171  (per-user cost + detail)
  * app/services/usage_log.py:133              (usage_summary for one user)
  * app/admin/web/services/book_usage.py:56    (book-generation ceiling)

The admin user-detail page issues three of those in one render, so the cost
of opening it grew linearly with total API calls ever made.

COMPOSITE, user_id FIRST: every one of those queries filters user_id for
equality and then either bounds or orders by created_at, which is exactly the
access pattern a (user_id, created_at) btree serves — the leading column
narrows to one user, the trailing one satisfies the range/sort without a
re-sort. The existing standalone created_at index is LEFT IN PLACE: the
dashboard's global time-window aggregates filter created_at with no user_id
predicate and cannot use a user_id-leading index.

INDEX ONLY — no column is added, altered or dropped, and no row is rewritten.

Revision ID: 025
Revises: 024
Create Date: 2026-08-29 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_gemini_usage_user_id_created_at"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "gemini_usage",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="gemini_usage")
