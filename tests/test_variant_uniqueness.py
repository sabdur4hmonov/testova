"""AUDIT B-17 — variant uniqueness, as it stands after CP-5.

History. Re-generating variants used to leave two rows with the same
`variant_number` and different shuffles, and grading picked the newest. The
first fix (migration 027) added a (project_id, variant_number) constraint and an
UPSERT — which stopped the duplicates but rewrote the delivered keys in place,
so printed papers were still graded against a key the teacher never received.
The tests that pinned that upsert asserted the defect and are gone.

CP-5 replaces both: a generation is an immutable EDITION with its own rows
(tests/test_exam_editions.py), so uniqueness is per edition. What remains here
is the uniqueness guarantee itself and the grading scope.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.project import Project, ProjectStatus
from app.models.user import User
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT


async def test_one_edition_cannot_hold_two_rows_for_the_same_number(session):
    from app.services.variant_store import create_edition

    owner = User(telegram_id=TELEGRAM_USER_ID_LIMIT + 521, full_name="Unique Owner")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="Unique", status=ProjectStatus.COMPLETED)
    session.add(project)
    await session.flush()

    twice = [{"variant_number": 1, "question_order": [], "option_mapping": {},
              "answer_key": {"1": [k]}} for k in ("A", "B")]
    await create_edition(session, project.id, twice, source="upload")
    with pytest.raises(IntegrityError):
        await session.flush()


def test_grading_selects_within_the_teachers_project():
    """Scoped to the project and teacher; variant_number restarts per project."""
    import inspect

    from app.bot.handlers import checking

    src = inspect.getsource(checking)
    assert "Variant.variant_number == variant_num" in src
    assert "Project.user_id == db_user.id" in src
