"""Access control: user gating columns, builder use_charged, admin_log

Revision ID: 003
Revises: 002
Create Date: 2026-07-10 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users — existing rows get NULL/NULL (unlimited) so nobody is locked out
    op.add_column("users", sa.Column("access_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("uses_left", sa.Integer(), nullable=True))
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("users", sa.Column("note", sa.Text(), nullable=True))

    # builder_sessions — one-use-per-session charge flag
    op.add_column(
        "builder_sessions",
        sa.Column("use_charged", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # admin_log
    op.create_table(
        "admin_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target", sa.BigInteger(), nullable=True),
        sa.Column(
            "params", postgresql.JSONB, nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("admin_log")
    op.drop_column("builder_sessions", "use_charged")
    for col in ("note", "is_blocked", "is_admin", "uses_left", "access_until"):
        op.drop_column("users", col)
