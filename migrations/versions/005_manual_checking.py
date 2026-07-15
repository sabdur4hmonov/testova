"""Manual answer-sheet checking: check_results + manual_check_sessions

Revision ID: 005
Revises: 004
Create Date: 2026-07-15 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manual_check_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column(
            "correct_answers", postgresql.JSONB(),
            nullable=False, server_default="{}",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "check_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "manual_session_id", postgresql.UUID(as_uuid=True),
            nullable=True, index=True,
        ),
        sa.Column("variant_number", sa.Integer(), nullable=True),
        sa.Column("student_name", sa.String(100), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "wrong_answers", postgresql.JSONB(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "unclear", postgresql.JSONB(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("check_results")
    op.drop_table("manual_check_sessions")
