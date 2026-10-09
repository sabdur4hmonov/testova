"""book_themes.kind: separate structural GROUP HEADERS from real content sections

STRICTLY ADDITIVE — one new column with a server default, so every existing row
becomes 'section' (exactly today's behaviour) and nothing is rewritten.

WHY: a textbook's table of contents interleaves chapter-level headers
("Глава II. Клетка – основа жизни") with the numbered sections under them
("§ 3. ...", "§ 4. ..."). Before this, both became equal book_themes rows, so a
59-section book produced 65 themes. The damage was not the 6 extra rows but the
NUMBERING: themes are selected by number, so the teacher's "§ 12" was not
theme 12. Headers also became degenerate one-page themes.

Headers are now kept for display (they group the list a teacher scrolls) but are
NOT selectable, NOT counted, and take no part in page-range computation.

Revision ID: 016
Revises: 015
Create Date: 2026-08-15 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "book_themes",
        sa.Column("kind", sa.String(16), nullable=False, server_default="section"),
    )


def downgrade() -> None:
    op.drop_column("book_themes", "kind")
