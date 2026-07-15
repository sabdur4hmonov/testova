"""Project naming + reserved exam-scheduling columns

Revision ID: 006
Revises: 005
Create Date: 2026-07-15 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("display_name", sa.String(100), nullable=True))
    op.add_column("projects", sa.Column("checking_mode", sa.String(20), nullable=True))
    op.add_column(
        "projects", sa.Column("exam_start_time", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "projects", sa.Column("exam_end_time", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "projects", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("projects", "expires_at")
    op.drop_column("projects", "exam_end_time")
    op.drop_column("projects", "exam_start_time")
    op.drop_column("projects", "checking_mode")
    op.drop_column("projects", "display_name")
