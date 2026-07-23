"""Question.is_deleted — soft-delete flag (SEPARATE from any active/status concept)

Additive ONLY: adds questions.is_deleted (Boolean, NOT NULL, default false). A
deleted question is excluded from EVERY read site (generation, key entry, PDFs,
grading, summaries) but its row is kept so nothing renumbers and history stays
intact. This is its own concept — it is never conflated with subscription
is_active or project status. Old rows default to false (not deleted).

Revision ID: 009
Revises: 008
Create Date: 2026-07-19 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("questions", "is_deleted")
