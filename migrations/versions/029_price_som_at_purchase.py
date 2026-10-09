"""Add users.price_som_at_purchase — what the teacher actually paid.

Pricing moved Standart 50,000 -> 75,000 and Pro 100,000 -> 150,000 (commit
3572928). `price_som` is a constant on a frozen dataclass, not a column, and
the account page derives the tier from the user's limits and then prints
today's constant — so every teacher who bought at the old price is told their
plan costs the new one.

Nothing is CHARGED by this: payment is manual and no code path moves money.
The column exists so the account page can show what a teacher actually paid.

ADDITIVE, AND DELIBERATELY NOT BACKFILLED. One nullable column, no server
default, no data write, no table rewrite. NULL means "sold before this column
existed" and the read falls back to the live constant.

Not backfilled because nothing in the schema can date a purchase: `period_start`
is rewritten by BOTH set_plan and reset_quota, so a renewal makes an August sale
indistinguishable from a September one. Seeding old prices off it would grandfather
the wrong people and un-grandfather the right ones. Re-running /plan on a
customer is how an admin grandfathers them — a decision, recorded, rather than
a guess baked into a migration.

Revision ID: 029
Revises: 028
Create Date: 2026-09-10 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("price_som_at_purchase", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    # Loses which teachers were grandfathered; the account page falls back to
    # the live constant for everyone, i.e. exactly the pre-029 behaviour.
    op.drop_column("users", "price_som_at_purchase")
