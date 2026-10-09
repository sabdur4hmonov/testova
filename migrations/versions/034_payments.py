"""Add `payments` — an append-only ledger of individual payment events.

WHAT THE SCHEMA COULD NOT ANSWER BEFORE THIS
--------------------------------------------
Migration 029 said it in its own docstring: "nothing in the schema can date a
purchase". `users.period_start` is rewritten by BOTH set_plan and reset_quota,
so a renewal makes an August sale indistinguishable from a September one, and
`users.price_som_at_purchase` records the LAST price stamped, overwritten by
the next sale. There was therefore no way to ask "how much did we collect this
month", "which teachers have ever paid", or "who paid before and has not
renewed" — the three questions the revenue page and two broadcast segments are
built on.

NEW TABLE, NOT `subscriptions`. The full reasoning lives in
app/models/payment.py's docstring (it is a design decision, not a migration
detail, so it belongs with the model). In short: `subscriptions` is dead (no
writer, 0 rows), it is a current-state record whose state `users` already owns
authoritatively, and its `plan` column is a legacy free/pro/center enum.
`subscriptions` is left exactly as it is — this migration does not touch it.

ADDITIVE ONLY — one new table. No existing column is added, altered or
dropped, no existing row is read or rewritten, and nothing outside this table
changes. Safe to apply to a live database with the bot running; the ledger is
empty until an admin records the first payment, and no access or quota check
consults it.

`plan_key` is String(16), NOT an enum: plans.py is the single source of truth
for which tiers exist, and a new tier must not require an ALTER TYPE on a live
database. A guard test pins the column's contents to plans.PLANS.keys().

Revision ID: 034
Revises: 033
Create Date: 2026-09-16 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_key", sa.String(16), nullable=False),
        sa.Column("amount_som", sa.Integer(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("method", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    # (user_id, paid_at) — the per-user history on the detail page, and the
    # "latest payment" lookup that answers "is this teacher late?".
    op.create_index("ix_payments_user_id_paid_at", "payments", ["user_id", "paid_at"])
    # (paid_at) — the revenue overview's period totals, which have no user
    # predicate at all and so cannot use the composite above.
    op.create_index("ix_payments_paid_at", "payments", ["paid_at"])


def downgrade() -> None:
    op.drop_index("ix_payments_paid_at", table_name="payments")
    op.drop_index("ix_payments_user_id_paid_at", table_name="payments")
    op.drop_table("payments")
