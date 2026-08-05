"""Add group_id and group_context columns to questions

Revision ID: 013
Revises: 012
Create Date: 2026-08-05 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("group_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "questions",
        sa.Column("group_context", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("questions", "group_context")
    op.drop_column("questions", "group_id")