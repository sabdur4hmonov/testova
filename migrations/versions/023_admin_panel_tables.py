"""Admin panel: credit_baseline + billing_reconciliation

Revision ID: 022
Revises: 021
Create Date: 2026-08-22 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credit_baseline",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("credit_usd", sa.Float(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False, index=True),
    )
    op.create_table(
        "billing_reconciliation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_label", sa.String(64), nullable=False),
        sa.Column("google_total_usd", sa.Float(), nullable=False),
        sa.Column("our_computed_usd", sa.Float(), nullable=False),
        sa.Column("delta_usd", sa.Float(), nullable=False),
        sa.Column("delta_pct", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("billing_reconciliation")
    op.drop_table("credit_baseline")
