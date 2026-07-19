"""Question options as label-preserving JSON (KNOWN-OPEN #2)

Additive ONLY: adds questions.options (JSONB), a list of {"letter","text"} that
preserves the paper's REAL option labels and order (Latin OR Cyrillic, any gaps,
any count). The legacy option_a..option_d columns STAY — old rows have
options=NULL and are read via a model fallback, so nothing needs backfilling and
old rows behave exactly as before. Dropping option_a..d is a future migration.

Revision ID: 007
Revises: 006
Create Date: 2026-07-19 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("options", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("questions", "options")
