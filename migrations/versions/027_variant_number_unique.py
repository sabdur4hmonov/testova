"""Unique (project_id, variant_number) on variants — AUDIT B-17.

Regenerating variants used to leave two rows with the same variant_number and
different shuffles; grading picked whichever was newest, so papers printed from
the first run were graded against the second run's key.

DEDUPE BEFORE CONSTRAINING. A database that already holds duplicates cannot
take the constraint, so this migration first collapses each
(project_id, variant_number) group to its NEWEST row — the one grading was
already selecting, so this changes no grading outcome; it only removes the rows
that were unreachable and ambiguous.

Any submissions attached to a removed duplicate are RE-POINTED at the surviving
row rather than deleted (the FK cascades, so a bare DELETE would silently
destroy grading history). Their stored results are self-contained snapshots, so
re-pointing does not alter what any past grade says.

Revision ID: 027
Revises: 026
"""
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Move submissions off the losing duplicates onto the surviving (newest)
    #    row for their (project_id, variant_number) group.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, project_id, variant_number,
                   first_value(id) OVER (
                       PARTITION BY project_id, variant_number
                       ORDER BY created_at DESC, id DESC
                   ) AS keeper
            FROM variants
        )
        UPDATE submissions s
        SET variant_id = r.keeper
        FROM ranked r
        WHERE s.variant_id = r.id AND r.id <> r.keeper
        """
    )

    # 2. Drop the losing duplicates.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY project_id, variant_number
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM variants
        )
        DELETE FROM variants v
        USING ranked r
        WHERE v.id = r.id AND r.rn > 1
        """
    )

    # 3. Now the constraint can be added.
    op.create_unique_constraint(
        "uq_variants_project_number", "variants", ["project_id", "variant_number"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_variants_project_number", "variants", type_="unique")
