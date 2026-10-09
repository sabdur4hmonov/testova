"""Book library OPT6 — question pool: book_question_pool + book_pool_served.

STRICTLY ADDITIVE. Two new tables; no existing table/column is altered, renamed
or dropped, and no existing row is rewritten. Both tables are read/written ONLY
when settings.BOOK_QUESTION_POOL_ENABLED is on (default off), so a deploy of
this migration alone changes no existing behaviour.

book_question_pool  — previously-generated, grounded questions, stored
                      canonically (label-free ordered option values + correct
                      index) so one row can serve any label style with the same
                      option count. UNIQUE dedup key keeps it from ballooning.
book_pool_served    — never-repeat ledger: which pooled item a teacher already
                      got for a theme, so it is never served to them twice.

downgrade() drops both cleanly, indexes and constraints included, in FK order
(the served ledger references the pool, so it goes first) — the same
drop-in-reverse rollback every other migration in this project provides.

Revision ID: 019
Revises: 018
Create Date: 2026-08-18 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "book_question_pool",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "theme_id", UUID(as_uuid=True),
            sa.ForeignKey("book_themes.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("difficulty", sa.String(length=10), nullable=False),
        sa.Column("is_open_ended", sa.Boolean(), nullable=False),
        sa.Column(
            "option_count", sa.Integer(),
            server_default="0", nullable=False,
        ),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("option_values", JSONB(), nullable=False),
        sa.Column("correct_index", sa.Integer(), nullable=True),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("question_hash", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "theme_id", "difficulty", "is_open_ended", "option_count",
            "question_hash", name="uq_book_question_pool_dedup",
        ),
    )
    op.create_index(
        "ix_book_question_pool_lookup", "book_question_pool",
        ["theme_id", "difficulty", "is_open_ended", "option_count"],
    )

    op.create_table(
        "book_pool_served",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "theme_id", UUID(as_uuid=True),
            sa.ForeignKey("book_themes.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "pool_item_id", UUID(as_uuid=True),
            sa.ForeignKey("book_question_pool.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "served_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "telegram_id", "pool_item_id", name="uq_book_pool_served_once",
        ),
    )
    op.create_index(
        "ix_book_pool_served_lookup", "book_pool_served",
        ["telegram_id", "theme_id"],
    )


def downgrade() -> None:
    # Served ledger first — it FKs the pool table.
    op.drop_index("ix_book_pool_served_lookup", table_name="book_pool_served")
    op.drop_table("book_pool_served")
    op.drop_index("ix_book_question_pool_lookup", table_name="book_question_pool")
    op.drop_table("book_question_pool")
