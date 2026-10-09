"""Book library CP31a: books.kind — narrative textbook / grammar workbook /
vocabulary list, deciding the natural question shape.

STRICTLY ADDITIVE. One new column on `books` with a 'narrative' server default,
so every existing book reads as 'narrative' and behaves exactly as before — no
existing column is altered/renamed/dropped and no row is rewritten.

Revision ID: 021
Revises: 020
Create Date: 2026-08-22 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "books",
        sa.Column("kind", sa.String(length=16), nullable=False,
                  server_default="narrative"),
    )


def downgrade() -> None:
    op.drop_column("books", "kind")
