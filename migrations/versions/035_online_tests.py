"""Online tests: online_tests, online_attempts, online_attempt_questions, online_events.

What the tables hold is documented on the models (app/models/online_test.py);
this migration creates exactly those tables, columns, nullability and indexes
— tests/test_migration_035_online_tests.py compares the two.

ADDITIVE ONLY. Four new tables; no existing table, column or row is touched.
`exam_editions` / `variants` are not referenced at all: an online test draws
from a project's `questions` directly. Safe to apply with the bot running,
and nothing reads these tables while ONLINE_TEST_ENABLED is false.

Two PARTIAL unique indexes carry rules the code relies on:
  * uq_online_attempts_live_name — one live attempt per student name per test
    (an annulled attempt does not count, which is how a retake is allowed);
  * uq_online_aq_live_position — one active question per position (a replaced
    row keeps its position for the record).

Revision ID: 035
Revises: 034
Create Date: 2026-10-03 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "online_tests",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("total_seconds", sa.Integer(), nullable=True),
        sa.Column("per_question_seconds", sa.Integer(), nullable=True),
        sa.Column("allow_back", sa.Boolean(), nullable=False),
        sa.Column("pool_question_ids", JSONB, nullable=False),
        sa.Column("replacement_enabled", sa.Boolean(), nullable=False),
        sa.Column("lang", sa.String(4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("code", name="uq_online_tests_code"),
    )
    op.create_index("ix_online_tests_project_id", "online_tests", ["project_id"])
    op.create_index("ix_online_tests_user_id", "online_tests", ["user_id"])

    op.create_table(
        "online_attempts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("test_id", UUID, sa.ForeignKey("online_tests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("first_name", sa.String(64), nullable=False),
        sa.Column("last_name", sa.String(64), nullable=False),
        sa.Column("class_name", sa.String(32), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("violations", sa.Integer(), nullable=False),
        sa.Column("away_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_position", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("max_score", sa.Integer(), nullable=True),
        sa.Column("annulled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_online_attempts_token_hash"),
    )
    op.create_index("ix_online_attempts_test_id", "online_attempts", ["test_id"])
    op.create_index(
        "uq_online_attempts_live_name", "online_attempts", ["test_id", "name_key"],
        unique=True, postgresql_where=sa.text("status <> 'annulled'"),
    )

    op.create_table(
        "online_attempt_questions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("attempt_id", UUID, sa.ForeignKey("online_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("question_id", UUID, sa.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("option_order", JSONB, nullable=False),
        sa.Column("answer", JSONB, nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shown_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
    )
    op.create_index("ix_online_attempt_questions_attempt_id", "online_attempt_questions", ["attempt_id"])
    op.create_index(
        "uq_online_aq_live_position", "online_attempt_questions", ["attempt_id", "position"],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "online_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("attempt_id", UUID, sa.ForeignKey("online_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("counted", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_online_events_attempt_id", "online_events", ["attempt_id"])


def downgrade() -> None:
    # Children first; dropping a table drops its indexes with it.
    op.drop_table("online_events")
    op.drop_table("online_attempt_questions")
    op.drop_table("online_attempts")
    op.drop_table("online_tests")
