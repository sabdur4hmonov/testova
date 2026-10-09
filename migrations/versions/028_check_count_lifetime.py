"""Add users.check_count_lifetime — Bepul's answer-sheet checks are LIFETIME.

The final pricing tiers give the free trial exactly 2 answer-sheet checks
*ever*, not 2 per month. That shape does not exist in the quota model today:
all four period counters hang off a single `period_start`, and
`_apply_rolling_reset` zeroes every one of them together, by design ("a counter
left out here would never reset and would permanently lock its dimension").
So a trial capped at 2 through `check_count_this_period` would quietly hand out
2 more checks on every 30-day rollover.

Rather than teach the reset to skip one counter for one tier — magic at a
distance in the one function whose docstring warns against exactly that — this
adds a counter that no reset ever touches. `plans.is_trial` decides which of
the two a given user's CHECK quota reads.

Trial GENERATION needs no column: `uses_left` is already a lifetime meter
(written once by apply_trial, only ever decremented, excluded from every
reset), so the shared 1-generation cap rides that.

ADDITIVE. One column, NOT NULL with a server default of 0, so existing rows
take the default without a table rewrite and no read path can see a NULL.

The one data write is a SEED, not a rewrite: existing trial users already have
checks recorded in `check_count_this_period`, and starting their lifetime
counter at 0 would silently gift them their whole trial allowance back. The
seed carries that history across for trial rows only (paid rows keep using the
rolling counter, so their lifetime counter must stay 0).

Revision ID: 028
Revises: 027
Create Date: 2026-09-04 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "check_count_lifetime",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # Seed the trial rows from the counter they were using until now. The
    # predicate is the same one plans.is_trial uses: no paid test pool, and a
    # live trial meter. A fully-unconfigured legacy row (uses_left NULL) is NOT
    # trial and is left at 0, matching the fail-open reading everywhere else.
    op.execute(
        """
        UPDATE users
           SET check_count_lifetime = check_count_this_period
         WHERE monthly_variant_limit IS NULL
           AND uses_left IS NOT NULL
           AND is_admin = false
        """
    )


def downgrade() -> None:
    # Dropping the column loses the trial-check history it accumulated; the
    # rolling counter it was seeded from is still there, so a downgrade falls
    # back to monthly-resetting trial checks (the pre-028 behaviour).
    op.drop_column("users", "check_count_lifetime")
