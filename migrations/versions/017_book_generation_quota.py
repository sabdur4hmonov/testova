"""Book library: the question-GENERATION quota meter (Phase 3 wiring)

STRICTLY ADDITIVE. Two new nullable/defaulted columns on `users`; no existing
column is altered, renamed or dropped, and no existing row is rewritten.

Generation is a genuinely different paid Gemini cost from book ANALYSIS (014's
monthly_book_analysis_limit): analysis is a one-time TOC read per new book,
generation is the reasoning call that runs every time a teacher asks for
questions from an already-saved theme, and unlike analysis it has NO free
tier (see book_generator.py's module docstring — CP14 removed the free-key
path outright). Reusing BOOK_ANALYSIS's counter for it would blur two
distinct costs into one number; this migration gives generation its own
rolling-monthly meter instead, sharing users.period_start with the others.

NULL limit = unlimited (same convention as 012 and 014), so nobody is newly
gated on deploy. The counter defaults to 0.

Revision ID: 017
Revises: 016
Create Date: 2026-08-17 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("monthly_book_generation_limit", sa.Integer(), nullable=True)
    )
    op.add_column("users", sa.Column(
        "book_generation_count_this_period", sa.Integer(), nullable=False,
        server_default="0",
    ))


def downgrade() -> None:
    op.drop_column("users", "book_generation_count_this_period")
    op.drop_column("users", "monthly_book_generation_limit")
