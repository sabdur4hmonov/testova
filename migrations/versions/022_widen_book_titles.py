"""Widen books.title and book_themes.title from VARCHAR(512) to TEXT.

WHY: a garbled/OCR contents page can merge many TOC lines into one runaway
"title". A real Grammarway 2 upload produced a 1494-char theme title, which
overflowed the VARCHAR(512) column and raised StringDataRightTruncationError at
insert — crashing the whole upload. The analyzer now routes such a book to the
Vision tier and book_library.clamp_title trims to a sane length, but the column
itself is widened to TEXT as defense in depth so no unforeseen garbling can ever
crash the insert again.

STRICTLY ADDITIVE / WIDENING: TEXT is a superset of VARCHAR(512), so every
existing value is preserved unchanged and no row is rewritten. Downgrade narrows
back to VARCHAR(512); clamp_title keeps stored titles <= 500 chars, so the
downgrade cast cannot lose data in practice, but it is a genuine narrowing and is
only safe while that clamp holds.

Revision ID: 022
Revises: 021
Create Date: 2026-08-24 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None

_TABLES = ("books", "book_themes")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table, "title",
            existing_type=sa.String(length=512),
            type_=sa.Text(),
            existing_nullable=False,
        )


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table, "title",
            existing_type=sa.Text(),
            type_=sa.String(length=512),
            existing_nullable=False,
        )
