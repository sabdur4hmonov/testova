"""Book library CP24: book_theme_images — figures cropped from a theme's pages
for image-based question generation.

STRICTLY ADDITIVE. One new table; no existing table/column is altered, renamed
or dropped, and no existing row is rewritten. Nothing reads this table unless a
teacher opts into image questions, so deploy changes no existing behaviour.

Two image paths per row (cost control): image_path is the high-DPI crop for the
final PDF; gen_image_path is a ≤384px downscaled copy sent to Gemini for
generation (flat 258-token rate vs tiled pricing).

Revision ID: 018
Revises: 017
Create Date: 2026-08-18 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "book_theme_images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "theme_id", UUID(as_uuid=True),
            sa.ForeignKey("book_themes.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("image_path", sa.String(length=1024), nullable=False),
        sa.Column("gen_image_path", sa.String(length=1024), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_book_theme_images_theme_order", "book_theme_images",
        ["theme_id", "order_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_book_theme_images_theme_order", table_name="book_theme_images")
    op.drop_table("book_theme_images")
