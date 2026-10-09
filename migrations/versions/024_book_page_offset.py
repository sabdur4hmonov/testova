"""Book library Item 2 (page-range test generation): the two additive columns
the page-number path needs.

STRICTLY ADDITIVE — no existing column is altered/renamed/dropped and no row is
rewritten:

  * books.page_offset (INTEGER, NULL) — the measured shift between a page's
    PRINTED number (footer/header) and its PDF file index, detected ONCE from the
    book's margins and reused. NULL means "not detected yet"; 0 is a legitimate
    detected value (printed == PDF index), so the column is nullable rather than
    defaulting to 0.
  * book_themes.is_page_range (BOOLEAN, NULL) — marks a SYNTHETIC theme created
    from a teacher's page range, so it can be excluded from the topic-selection /
    manage UI (which must stay byte-identical to before) while still flowing
    through the shared extract → cache → generate → pool pipeline. NULL / false
    on every pre-existing row, so the "By topic" flow is unchanged.

CHAINS OFF 023 (the current head) so the tree keeps a SINGLE alembic head and
`alembic upgrade head` stays unambiguous. 023 is the admin-panel migration; this
file only points its own down_revision at it and does not modify it. (An earlier
plan chained off 022 to decouple from admin, but with 023 present that leaves two
heads and breaks the real upgrade→downgrade migration test — a single head wins.)

Revision ID: 024
Revises: 023
Create Date: 2026-08-28 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "books",
        sa.Column("page_offset", sa.Integer(), nullable=True),
    )
    op.add_column(
        "book_themes",
        sa.Column("is_page_range", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("book_themes", "is_page_range")
    op.drop_column("books", "page_offset")
