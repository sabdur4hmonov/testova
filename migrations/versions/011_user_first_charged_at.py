"""users.first_charged_at: timestamp of a user's first-ever charged use

Additive, nullable. Marks the moment a user consumed their first charged use so
the admin trial-conversion notification fires exactly once, ever. NULL = never
charged yet. Existing users are NULL (they may or may not have charged before
this existed — we deliberately do NOT backfill, so no false "first use" alert
fires for an already-active teacher).

Revision ID: 011
Revises: 010
Create Date: 2026-07-30 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("first_charged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "first_charged_at")
