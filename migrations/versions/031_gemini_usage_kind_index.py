"""Index gemini_usage(kind, user_id) — the book-usage analysis tally.

AUDIT C-27. app/admin/web/services/book_usage.py:55-59 counts book-ANALYSIS
passes per teacher:

    SELECT user_id, count(*) FROM gemini_usage
     WHERE user_id IS NOT NULL AND kind IN ('book_toc','book_toc_photo')
     GROUP BY user_id

`kind` was indexed nowhere, so this fell back to a sequential scan of the
fastest-growing table in the schema (one row per Gemini API call). Measured on
the dev DB before this migration: Seq Scan, 129 buffers, 8,507 of 8,793 rows
discarded by the filter to produce 108 groups — i.e. the cost of opening the
book-usage page grows with total API calls ever made, while the answer it
needs comes from ~6% of them.

COMPOSITE, kind FIRST: the query filters `kind` with IN (equality against a
small set) and then groups by `user_id`. A (kind, user_id) btree narrows on
the leading column and carries the grouping column with it, so the aggregate
can be satisfied from the index — count(*) needs no other column from the
heap. The reverse order would not help: `user_id` here has no equality
predicate at all, only IS NOT NULL.

The existing (user_id, created_at) index from migration 025 cannot serve this
query — it leads with a column this query does not filter for equality — and
is left in place for the per-user cost reads it was built for.

DELIBERATELY NOT WINDOWED. C-27 also proposed bounding this query to a recent
time window. That was rejected on purpose: the value it feeds is
`analysis_total`, rendered under the column header "Tahlil (jami)" ("Analysis
(total)") and documented as a LIFETIME count, sitting directly beside the
30-day meter "Tahlil (oy)". Adding a window would have made the two columns
duplicate each other and made the header a lie. The index removes the scan
cost without redefining the number.

INDEX ONLY — no column is added, altered or dropped, and no row is rewritten.

Revision ID: 031
Revises: 030
Create Date: 2026-09-11 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_gemini_usage_kind_user_id"


def upgrade() -> None:
    op.create_index(INDEX_NAME, "gemini_usage", ["kind", "user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="gemini_usage")
