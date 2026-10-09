"""book_access: a teacher's library may include books they did not upload

STRICTLY ADDITIVE — one new table, nothing existing is touched.

WHY: before this, "Eski kitoblardan" listed books by books.user_id, i.e. only
the ones a teacher uploaded themselves. A teacher who accepted the duplicate
offer ("✅ Shu kitobni ishlataman") got zero-cost access for that moment and
then lost the book — it never appeared in their library again. This join table
makes that grant persistent.

books.user_id stays the OWNER (who uploaded it, whose book_limit it counts
against, whose delete cascades it). book_access is a separate, weaker grant:
"this teacher may use this book". A teacher's library is the union of the two.

UNIQUE(user_id, book_id) so accepting the same book twice is idempotent — the
handler relies on that instead of a check-then-insert race.

Revision ID: 015
Revises: 014
Create Date: 2026-08-15 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "book_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column(
            "book_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("books.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint("user_id", "book_id", name="uq_book_access_user_book"),
    )


def downgrade() -> None:
    op.drop_table("book_access")
