"""Persisting generated variants — the ONE place that writes `variants` rows.

CP-5 (AUDIT B-17). Variants used to be UPSERTED: regenerating a project kept
each row id and replaced answer_key / question_order / option_mapping in
place. Proven through the real dispatcher: when the answer-key send failed after
the commit and the teacher typed the variant count again, the variants.pdf they
already had was left behind — every one of its keys replaced by a key nobody
received.

Every generation is now an EDITION (`ExamEdition`): one delivery owning its own
variant rows. `create_edition` only ever INSERTS. Nothing in the app assigns a
stored variant's grading columns, and the `variants_frozen` trigger refuses an
UPDATE that tries, so older editions stay exactly as delivered and gradeable.

SNAPSHOTS. Grading used to type each printed position (open vs multiple choice)
and the offered option labels from the live Question rows, which can still be
edited after printing. The edition freezes both at generation time:
`Variant.open_positions` and `ExamEdition.option_labels`. Variants handed in
without `questions_data` snapshot nothing (NULL), and grading falls back to the
live rows exactly as before.

The edition code is chosen BEFORE the PDFs are built, because it is printed on
them; the edition row itself is written only once the build succeeded, so an
edition never exists without its PDFs.
"""
from __future__ import annotations

import secrets
import uuid
from typing import Any, Iterable

from sqlalchemy import func, select

from app.models.exam_edition import ExamEdition
from app.models.project import Project
from app.models.variant import Variant
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Printed and copied by hand: no 0/O, 1/I/L.
EDITION_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
EDITION_CODE_LENGTH = 4


def _random_code() -> str:
    return "".join(secrets.choice(EDITION_CODE_ALPHABET) for _ in range(EDITION_CODE_LENGTH))


async def reserve_edition_code(session) -> str:
    """A code no edition uses yet. Two generations racing to the same one of
    ~920k codes is not expected; if it ever happens, the unique constraint makes
    the second commit fail loudly rather than share a code."""
    for _ in range(20):
        code = _random_code()
        taken = (await session.execute(
            select(ExamEdition.id).where(ExamEdition.code == code)
        )).first()
        if taken is None:
            return code
    raise RuntimeError("no free edition code found after 20 attempts")


def _usable_options(question: dict) -> list[str]:
    """Labels of the options actually printed for a question (blank dropped)."""
    options = question.get("options") or {}
    return [str(k).strip() for k, text in options.items()
            if str(k).strip() and text is not None and str(text).strip()]


def snapshot_open_positions(variant: dict[str, Any]) -> list[int] | None:
    """Printed positions with no options — the open (written) questions."""
    questions = variant.get("questions_data")
    if questions is None:
        return None
    return sorted(
        int(q.get("position_in_variant") or i)
        for i, q in enumerate(questions, start=1)
        if not _usable_options(q)
    )


def snapshot_option_labels(variants: list[dict[str, Any]]) -> list[str] | None:
    """The single-character labels an edition offers (any script, numeric too)."""
    if not any(v.get("questions_data") is not None for v in variants):
        return None
    labels = {
        label.upper()
        for v in variants for q in (v.get("questions_data") or [])
        for label in _usable_options(q)
        if len(label) == 1 and label.isalnum()
    }
    return sorted(labels)


async def create_edition(
    session,
    project_id: uuid.UUID | str,
    variants: Iterable[dict[str, Any]],
    *,
    source: str,
    title: str | None = None,
    pdf_format: str | None = None,
    code: str | None = None,
) -> ExamEdition:
    """Write `variants` as the project's NEXT edition. Never touches an existing
    variant. The caller commits."""
    if isinstance(project_id, str):
        project_id = uuid.UUID(project_id)
    variants = list(variants)

    # Serialise edition numbering per project: two generations for the same
    # project must not both claim the same edition_number.
    await session.execute(
        select(Project.id).where(Project.id == project_id).with_for_update()
    )
    last = (await session.execute(
        select(func.max(ExamEdition.edition_number))
        .where(ExamEdition.project_id == project_id)
    )).scalar()

    edition = ExamEdition(
        id=uuid.uuid4(),
        project_id=project_id,
        edition_number=(last or 0) + 1,
        code=code or await reserve_edition_code(session),
        source=source,
        variant_count=len(variants),
        title=title,
        pdf_format=pdf_format,
        option_labels=snapshot_option_labels(variants),
    )
    session.add(edition)
    # The edition row must exist before its variants' foreign keys land (the
    # app sessions run autoflush=False).
    await session.flush()

    for v in variants:
        session.add(Variant(
            project_id=project_id,
            edition_id=edition.id,
            variant_number=v["variant_number"],
            question_order=v["question_order"],
            option_mapping=v["option_mapping"],
            answer_key=v["answer_key"],
            open_positions=snapshot_open_positions(v),
        ))

    logger.info(
        "exam_edition_created",
        project_id=str(project_id), edition=edition.edition_number, code=edition.code,
        source=source, variants=len(variants),
    )
    return edition
