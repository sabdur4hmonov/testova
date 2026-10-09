"""Per-call-site cost telemetry: cached_tokens + call_site.

AUDIT Phase 1 item 1. Two gaps this closes:

  * cached_tokens — the REST response returns cachedContentTokenCount and the
    SDK exposes cached_content_token_count, but neither was ever read. Implicit
    prefix caching is therefore INVISIBLE: we cannot tell whether it ever hits,
    which makes the prompt-reordering work (Phase 1 item 7) unverifiable.
  * call_site — `kind` is too coarse. All four ai_analyzer call sites (page
    extraction, scheme transcription, option recovery, figure recovery) log the
    single kind "extract", so a per-call-site cost breakdown is impossible.

STRICTLY ADDITIVE. No column is altered, renamed or dropped and no row is
rewritten. cached_tokens takes a server_default of 0 so existing rows get a
real value rather than NULL; call_site is nullable because historical rows
genuinely do not know their site (backfilling a guess would be worse than
admitting the gap).

Revision ID: 026
Revises: 025
Create Date: 2026-08-29 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_gemini_usage_call_site_created_at"


def upgrade() -> None:
    op.add_column(
        "gemini_usage",
        sa.Column(
            "cached_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "gemini_usage", sa.Column("call_site", sa.String(64), nullable=True)
    )
    # Mirrors 025's (user_id, created_at): the breakdown queries filter one
    # call_site and then bound or order by time.
    op.create_index(
        INDEX_NAME, "gemini_usage", ["call_site", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="gemini_usage")
    op.drop_column("gemini_usage", "call_site")
    op.drop_column("gemini_usage", "cached_tokens")
