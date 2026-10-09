"""Book library CP28: questions.answer_rubric — expected key points for an
open-ended answer, produced at generation time for a FUTURE answer-checker.

STRICTLY ADDITIVE. One new NULLABLE column on `questions`; no existing column is
altered, renamed or dropped, and no existing row is rewritten (every existing
question reads answer_rubric as NULL). Nothing in today's grading path reads this
column — it is dormant data — so deploy changes no existing behaviour.

Revision ID: 020
Revises: 019
Create Date: 2026-08-22 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("answer_rubric", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("questions", "answer_rubric")
