"""sheet_checks — one durable row per answer-sheet photo (CP-4).

WHY. The checking flow kept every per-sheet fact (the read, the student name,
the resolved variant and key, the pending To'g'ri/Xato queue) in ONE shared
FSM record per chat, and walked each sheet through its own FSM sub-state
(name -> variant -> confirm). Proven with the real aiogram 3.20 dispatcher:

  * a photo sent while a sheet waited in a sub-state matched no handler and
    was dropped with no reply and no log line;
  * aiogram loads FSM state before our per-user media lock, so a second photo
    that arrived during the first photo's read passed the filter and then
    overwrote the first sheet's answers and name.

A row per photo makes each sheet's data its own. FSM keeps only the RUN
(flow, project or typed key, test name, run_id) and one state for the whole
run, so a photo always has a handler and never has anything to overwrite.

ADDITIVE ONLY: a new table and its indexes. No existing table, column or row
is touched, and downgrade drops only what upgrade created.

Revision ID: 032
Revises: 031
Create Date: 2026-09-14 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sheet_checks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("flow", sa.String(16), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("manual_session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("file_id", sa.String(256), nullable=True),
        sa.Column("sheet_key", sa.String(1024), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("read", JSONB(), nullable=True),
        sa.Column("student_name", sa.String(100), nullable=True),
        sa.Column("name_unclear", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("variant_number", sa.Integer(), nullable=True),
        sa.Column("variant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("answer_key", JSONB(), nullable=True),
        sa.Column("open_questions", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("confirm_pending", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("confirm_overrides", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("check_result_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sheet_checks_user_run_status", "sheet_checks",
                    ["user_id", "run_id", "status"], unique=False)
    op.create_index("ix_sheet_checks_user_prompt", "sheet_checks",
                    ["user_id", "prompt_message_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sheet_checks_user_prompt", table_name="sheet_checks")
    op.drop_index("ix_sheet_checks_user_run_status", table_name="sheet_checks")
    op.drop_table("sheet_checks")
