"""AI Textbook Library: books + book_themes, and the two book quota meters

STRICTLY ADDITIVE. Two new tables and three new nullable/defaulted columns on
`users`; no existing column is altered, renamed or dropped, and no existing row
is rewritten. Production has real users — the only effect of this migration on
them is three extra columns whose values are NULL / 0.

Nobody is newly gated on deploy: both limits arrive NULL, and NULL means
unlimited on that dimension (same convention as migration 012's monthly
quotas). The book counter defaults to 0.

`status` on both tables is a plain VARCHAR(20), NOT a native enum — see the
module docstring in app/models/book.py for why. It also keeps downgrade()
simple: no type to drop, no ordering hazard.

Revision ID: 014
Revises: 013
Create Date: 2026-08-14 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "books",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(128), nullable=True),
        sa.Column("grade", sa.String(32), nullable=True),
        sa.Column("page_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("original_pdf_path", sa.String(1024), nullable=False),
        sa.Column("has_toc", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("is_public", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("uploaded_by_user_id", sa.BigInteger, nullable=True),
        sa.Column("use_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pdf_checksum", sa.String(32), nullable=False, index=True),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    op.create_table(
        "book_themes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "book_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("books.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("order_index", sa.Integer, nullable=False),
        sa.Column("page_start", sa.Integer, nullable=False),
        sa.Column("page_end", sa.Integer, nullable=False),
        sa.Column("extracted_text", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    # Themes are always read as "this book's themes, in order".
    op.create_index(
        "ix_book_themes_book_id_order", "book_themes", ["book_id", "order_index"]
    )

    # ── users: the two book meters ───────────────────────────────────────────
    # book_limit is a TOTAL cap on books owned and never resets (a library is
    # permanent). The analysis limit + counter are a rolling-monthly meter that
    # shares users.period_start with the variant/check counters from 012.
    op.add_column("users", sa.Column("book_limit", sa.Integer(), nullable=True))
    op.add_column(
        "users", sa.Column("monthly_book_analysis_limit", sa.Integer(), nullable=True)
    )
    op.add_column("users", sa.Column(
        "book_analysis_count_this_period", sa.Integer(), nullable=False,
        server_default="0",
    ))


def downgrade() -> None:
    # Reverse order: the users columns first (independent), then the child
    # table, then the parent. Dropping `books` before `book_themes` would fail
    # on the FK.
    op.drop_column("users", "book_analysis_count_this_period")
    op.drop_column("users", "monthly_book_analysis_limit")
    op.drop_column("users", "book_limit")

    op.drop_index("ix_book_themes_book_id_order", table_name="book_themes")
    op.drop_table("book_themes")
    op.drop_table("books")
