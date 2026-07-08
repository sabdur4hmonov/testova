"""Multi-Source Builder session tables

Revision ID: 002
Revises: 001
Create Date: 2026-07-08 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    builder_status = postgresql.ENUM(
        "active", "finished", "saved", "cancelled", name="builder_status_enum"
    )
    builder_status.create(op.get_bind())

    op.create_table(
        "builder_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="builder_status_enum", create_type=False),
            nullable=False, server_default="active", index=True,
        ),
        sa.Column(
            "pool_project_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "builder_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("builder_sessions.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("question_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "image_question_count", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "warnings", postgresql.JSONB, nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "key_complete", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("builder_sources")
    op.drop_table("builder_sessions")
    postgresql.ENUM(name="builder_status_enum").drop(op.get_bind())
