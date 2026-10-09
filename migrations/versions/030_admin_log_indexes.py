"""Index admin_log for the audit page's two read shapes.

AUDIT C-26. admin_log is append-only and grows with every admin action and
every broadcast (2,709 rows on the dev DB at the time of writing), and it was
indexed on admin_id ONLY — a column no read in the panel filters on. Both of
the audit page's queries therefore fell back to a sequential scan plus a sort:

  * app/admin/web/routers/audit.py:36-40  — ORDER BY created_at DESC, paged
    with OFFSET/LIMIT (the unfiltered listing, and the count above it)
  * app/admin/web/routers/audit.py:33-35  — the same, narrowed by
    WHERE action = ? when the dropdown filter is used
  * app/admin/web/routers/exports.py:84   — ORDER BY created_at DESC for the
    audit CSV export

TWO INDEXES, for the two shapes, following the reasoning migration 025 spelled
out for gemini_usage:

  * (created_at) serves the unfiltered listing and the CSV export, which order
    by created_at with no other predicate;
  * (action, created_at) serves the filtered listing — equality on the leading
    column narrows to one action, and the trailing column then satisfies the
    ORDER BY without a re-sort. A standalone (action) index could not do that
    second half, and the (created_at) index cannot serve the filter.

The DISTINCT-action query that used to run on every page render is removed in
the same commit (it scanned the whole table to rebuild a fixed 18-value list),
so no index is needed for it.

INDEX ONLY — no column is added, altered or dropped, and no row is rewritten.

Revision ID: 030
Revises: 029
Create Date: 2026-09-11 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None

CREATED_AT_INDEX = "ix_admin_log_created_at"
ACTION_CREATED_AT_INDEX = "ix_admin_log_action_created_at"


def upgrade() -> None:
    op.create_index(CREATED_AT_INDEX, "admin_log", ["created_at"], unique=False)
    op.create_index(
        ACTION_CREATED_AT_INDEX, "admin_log", ["action", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(ACTION_CREATED_AT_INDEX, table_name="admin_log")
    op.drop_index(CREATED_AT_INDEX, table_name="admin_log")
