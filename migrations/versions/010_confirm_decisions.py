"""confirm_decisions: append-only log of teacher To'g'ri/Xato decisions

Additive ONLY: a new table capturing each teacher confirm decision on a wrong
written answer (student text, key, similarity that produced the ask, and the
verdict). NOT read by grading — ground truth for validating/tuning the
confirm-similarity threshold over time.

Revision ID: 010
Revises: 009
Create Date: 2026-07-26 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "confirm_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("flow", sa.String(16), nullable=False),
        sa.Column("question_number", sa.Integer(), nullable=False),
        sa.Column("student_answer", sa.String(100), nullable=True),
        sa.Column("correct_answer", sa.String(512), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("teacher_correct", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("confirm_decisions")
