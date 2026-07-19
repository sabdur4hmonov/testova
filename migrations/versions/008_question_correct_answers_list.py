"""Question.correct_answers as a JSONB list (accepted answers, incl. written)

Additive ONLY: adds questions.correct_answers (JSONB), a LIST of accepted answer
strings — a single letter is a one-item list ["A"], a written multi-accept is
["PHONE","TELEPHONE"]. Replaces the String(4) single-letter correct_answer for
new writes; the old column STAYS for fallback (correct_answers_ordered reads the
list, else [correct_answer]), so old rows need no backfill and grade unchanged.

Revision ID: 008
Revises: 007
Create Date: 2026-07-19 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("correct_answers", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("questions", "correct_answers")
