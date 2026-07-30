"""users monthly quotas: independent variant + check limits (rolling 30 days)

Additive, nullable limits (NULL = unlimited, so ALL existing users default to
unlimited and nobody is newly gated on deploy). Counters default 0. period_start
anchors each user's ROLLING 30-day window (reset happens lazily when the user
acts and 30 days have elapsed since period_start).

Revision ID: 012
Revises: 011
Create Date: 2026-07-30 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("monthly_variant_limit", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column(
        "variant_count_this_period", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("monthly_check_limit", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column(
        "check_count_this_period", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column(
        "period_start", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "period_start")
    op.drop_column("users", "check_count_this_period")
    op.drop_column("users", "monthly_check_limit")
    op.drop_column("users", "variant_count_this_period")
    op.drop_column("users", "monthly_variant_limit")
