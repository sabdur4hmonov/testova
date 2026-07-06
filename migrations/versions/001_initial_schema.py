"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-06-01 00:00:00.000000
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Enums ─────────────────────────────────────────────────────────────────
    language_enum = postgresql.ENUM("uz", "en", "ru", name="language_enum")
    language_enum.create(op.get_bind())

    plan_enum = postgresql.ENUM("free", "pro", "center", name="subscription_plan_enum")
    plan_enum.create(op.get_bind())

    status_enum = postgresql.ENUM(
        "pending", "processing", "completed", "failed", name="project_status_enum"
    )
    status_enum.create(op.get_bind())

    # ── users ─────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("telegram_id", sa.BigInteger, unique=True, nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("full_name", sa.String(256), nullable=False),
        sa.Column("language", sa.Enum("uz", "en", "ru", name="language_enum"), nullable=False, server_default="uz"),
        sa.Column("subscription_plan", sa.Enum("free", "pro", "center", name="subscription_plan_enum"), nullable=False, server_default="free"),
        sa.Column("daily_projects_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("monthly_projects_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_projects", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_reset_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_banned", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"])

    # ── subscriptions ─────────────────────────────────────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan", sa.Enum("free", "pro", "center", name="subscription_plan_enum"), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_reference", sa.String, nullable=True),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])

    # ── projects ──────────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("original_file_path", sa.String(1024), nullable=True),
        sa.Column("original_file_name", sa.String(512), nullable=True),
        sa.Column("file_type", sa.String(16), nullable=True),
        sa.Column("status", sa.Enum("pending", "processing", "completed", "failed", name="project_status_enum"), nullable=False, server_default="pending"),
        sa.Column("question_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("task_id", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])
    op.create_index("ix_projects_status", "projects", ["status"])

    # ── questions ─────────────────────────────────────────────────────────────
    op.create_table(
        "questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_number", sa.Integer, nullable=False),
        sa.Column("question_text", sa.Text, nullable=False),
        sa.Column("option_a", sa.Text, nullable=True),
        sa.Column("option_b", sa.Text, nullable=True),
        sa.Column("option_c", sa.Text, nullable=True),
        sa.Column("option_d", sa.Text, nullable=True),
        sa.Column("correct_answer", sa.String(4), nullable=True),
        sa.Column("has_image", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("image_path", sa.String(1024), nullable=True),
        sa.Column("image_description", sa.Text, nullable=True),
        sa.Column("page_number", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_questions_project_id", "questions", ["project_id"])

    # ── variants ──────────────────────────────────────────────────────────────
    op.create_table(
        "variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant_number", sa.Integer, nullable=False),
        sa.Column("question_order", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("option_mapping", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("answer_key", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("pdf_path", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_variants_project_id", "variants", ["project_id"])

    # ── submissions ───────────────────────────────────────────────────────────
    op.create_table(
        "submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("variants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_name", sa.String(256), nullable=True),
        sa.Column("answer_sheet_path", sa.String(1024), nullable=True),
        sa.Column("student_answers", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("results", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("correct_count", sa.Integer, nullable=True),
        sa.Column("wrong_count", sa.Integer, nullable=True),
        sa.Column("skipped_count", sa.Integer, nullable=True),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_submissions_variant_id", "submissions", ["variant_id"])


def downgrade() -> None:
    op.drop_table("submissions")
    op.drop_table("variants")
    op.drop_table("questions")
    op.drop_table("projects")
    op.drop_table("subscriptions")
    op.drop_table("users")

    op.execute("DROP TYPE project_status_enum")
    op.execute("DROP TYPE subscription_plan_enum")
    op.execute("DROP TYPE language_enum")
