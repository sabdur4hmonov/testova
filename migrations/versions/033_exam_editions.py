"""exam_editions — immutable printed editions of a test (CP-5, AUDIT B-17).

WHY. Migration 027 stopped duplicate (project_id, variant_number) rows, and the
writer then UPSERTED: regenerating a project rewrote answer_key / question_order
/ option_mapping in place. Proven through the real dispatcher: a teacher whose
answer-key send failed after the commit, and who typed the variant count again,
kept the delivered variants.pdf while every one of its keys was replaced — those
papers are graded against a key nobody received.

WHAT. An edition is one delivery. A regeneration is a NEW edition with NEW
variant rows; nothing rewrites a stored variant, and a trigger enforces that.

NOT PURELY ADDITIVE — approved explicitly for CP-5:
  * `uq_variants_project_number` is dropped: a second edition prints
    "Variant 1" again. It is replaced by (edition_id, variant_number).
  * `variants.edition_id` becomes NOT NULL after the backfill.
  * the `variants_frozen` trigger refuses UPDATEs of grading columns.

DEPLOY: stop the bot BEFORE running this. Old code inserts variants without an
edition_id and would fail against the NOT NULL column; this is not a rolling
migration.

BACKFILL. Every project that has variants gets ONE `legacy` edition (number 1,
dated from its earliest variant, with a fresh code that was never printed).
Every variant is linked to it; saved check_results are linked by project and
sheet_checks by variant. Submissions are not touched — they reference variants,
which keep their ids. Snapshots (open_positions, option_labels) stay NULL for
legacy rows, so grading them is unchanged: built from the live questions.

DOWNGRADE refuses while any project has more than one edition — restoring the
per-project constraint would require deleting a delivered edition.

Revision ID: 033
Revises: 032
Create Date: 2026-09-14 00:00:00.000000
"""
from __future__ import annotations

import secrets
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None

# Same alphabet as app.services.variant_store.EDITION_CODE_ALPHABET (no 0/O/1/I/L).
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

_FROZEN_FUNCTION = """
CREATE OR REPLACE FUNCTION variants_frozen() RETURNS trigger AS $$
BEGIN
    IF NEW.answer_key IS DISTINCT FROM OLD.answer_key
       OR NEW.question_order IS DISTINCT FROM OLD.question_order
       OR NEW.option_mapping IS DISTINCT FROM OLD.option_mapping
       OR NEW.variant_number IS DISTINCT FROM OLD.variant_number
       OR NEW.open_positions IS DISTINCT FROM OLD.open_positions
       OR NEW.edition_id IS DISTINCT FROM OLD.edition_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id THEN
        RAISE EXCEPTION 'variants are immutable - a delivered paper is graded against this row'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""
_FROZEN_TRIGGER = (
    "CREATE TRIGGER variants_frozen BEFORE UPDATE ON variants "
    "FOR EACH ROW EXECUTE FUNCTION variants_frozen()"
)


def upgrade() -> None:
    op.create_table(
        "exam_editions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("edition_number", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(8), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("variant_count", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("pdf_format", sa.String(16), nullable=True),
        sa.Column("option_labels", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("project_id", "edition_number", name="uq_exam_editions_project_number"),
        sa.UniqueConstraint("code", name="uq_exam_editions_code"),
    )
    op.create_index("ix_exam_editions_project_id", "exam_editions", ["project_id"])

    op.add_column("variants", sa.Column("edition_id", UUID(as_uuid=True), nullable=True))
    op.add_column("variants", sa.Column("open_positions", JSONB(), nullable=True))
    op.create_foreign_key("fk_variants_edition_id", "variants", "exam_editions",
                          ["edition_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_variants_edition_id", "variants", ["edition_id"])
    op.add_column("check_results", sa.Column("edition_id", UUID(as_uuid=True), nullable=True))
    op.add_column("sheet_checks", sa.Column("edition_id", UUID(as_uuid=True), nullable=True))

    # ── backfill: one legacy edition per project that has variants ──────────
    bind = op.get_bind()
    groups = bind.execute(sa.text(
        "SELECT project_id, count(*), min(created_at) FROM variants GROUP BY project_id"
    )).all()
    used: set[str] = set()
    for project_id, count, first_at in groups:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
        while code in used:
            code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
        used.add(code)
        bind.execute(sa.text(
            "INSERT INTO exam_editions (id, project_id, edition_number, code, source, "
            "variant_count, created_at) VALUES (:id, :p, 1, :code, 'legacy', :n, :at)"
        ), {"id": uuid.uuid4(), "p": project_id, "code": code, "n": count, "at": first_at})

    op.execute(
        "UPDATE variants v SET edition_id = e.id FROM exam_editions e "
        "WHERE e.project_id = v.project_id"
    )
    op.execute(
        "UPDATE check_results c SET edition_id = e.id FROM exam_editions e "
        "WHERE c.project_id IS NOT NULL AND e.project_id = c.project_id"
    )
    op.execute(
        "UPDATE sheet_checks s SET edition_id = v.edition_id FROM variants v "
        "WHERE s.variant_id = v.id"
    )

    op.alter_column("variants", "edition_id", nullable=False)
    op.create_unique_constraint("uq_variants_edition_number", "variants",
                                ["edition_id", "variant_number"])
    op.drop_constraint("uq_variants_project_number", "variants", type_="unique")

    # Installed last: the backfill above had to write edition_id.
    op.execute(_FROZEN_FUNCTION)
    op.execute(_FROZEN_TRIGGER)


def downgrade() -> None:
    bind = op.get_bind()
    multi = bind.execute(sa.text(
        "SELECT count(*) FROM (SELECT project_id FROM exam_editions "
        "GROUP BY project_id HAVING count(*) > 1) x"
    )).scalar()
    if multi:
        raise RuntimeError(
            f"refusing to downgrade 033: {multi} project(s) have more than one edition, "
            "and restoring uq_variants_project_number would mean deleting a delivered edition"
        )

    op.execute("DROP TRIGGER IF EXISTS variants_frozen ON variants")
    op.execute("DROP FUNCTION IF EXISTS variants_frozen()")
    op.create_unique_constraint("uq_variants_project_number", "variants",
                                ["project_id", "variant_number"])
    op.drop_constraint("uq_variants_edition_number", "variants", type_="unique")
    op.drop_column("sheet_checks", "edition_id")
    op.drop_column("check_results", "edition_id")
    op.drop_index("ix_variants_edition_id", table_name="variants")
    op.drop_constraint("fk_variants_edition_id", "variants", type_="foreignkey")
    op.drop_column("variants", "open_positions")
    op.drop_column("variants", "edition_id")
    op.drop_index("ix_exam_editions_project_id", table_name="exam_editions")
    op.drop_table("exam_editions")
