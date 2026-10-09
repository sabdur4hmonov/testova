"""CP-5 (1/2) — immutable exam editions.

AUDIT B-17, as it really stood. Migration 027 stopped DUPLICATE variant rows,
but `variant_store.upsert_variants` then rewrote answer_key / question_order /
option_mapping IN PLACE. Proven through the real dispatcher before this change:
the upload flow's variant-count step stays in `waiting_for_variant_count` until
its very last line, so

  * a teacher whose answer_keys.pdf send failed AFTER the commit (variants.pdf
    already delivered) and who typed the count again got the same row ids with
    three new keys — the delivered paper is graded against a key nobody has;
  * two overlapping count messages did the same (see
    test_upload_count_single_flight.py for that half).

An EDITION is one delivery: one generate -> PDFs -> send, owning its variant
rows. Editions are never rewritten — a regeneration is a new edition — and a
database trigger refuses any UPDATE of a stored key, so a future code path
cannot bring the overwrite back. Each edition carries a short code printed next
to "Variant N" and in the answer key, so a paper can be matched to its key.

Grading still selects the newest variant here; resolving a sheet to ITS edition
is CP-5 (2/2).
"""
from __future__ import annotations

import inspect
import json
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import app.bot.handlers.upload as U
from app.bot.states.forms import UploadStates
from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.models.variant import Variant
from tests.sheet_harness import (  # noqa: F401
    DocTelegram, build_dispatcher, fsm, patch_generation, sheet_env, text_update,
)
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT

APP = Path(__file__).resolve().parents[1] / "app"


def _j(value) -> str:
    return json.dumps(value, sort_keys=True)


async def _stored(env, project_id) -> set[tuple[int, str]]:
    async with env.sm() as s:
        rows = (await s.execute(
            select(Variant.variant_number, Variant.answer_key)
            .where(Variant.project_id == uuid.UUID(project_id))
        )).all()
    return {(n, _j(k)) for n, k in rows}


# ── the proven overwrite, through the real dispatcher ───────────────────────

async def test_a_retry_after_a_failed_send_keeps_the_first_delivery_gradeable(sheet_env):
    env = sheet_env
    project_id = await env.seed_mc_project()
    spy = patch_generation(env, U)
    dp, bot, _ = build_dispatcher(env)
    tg = DocTelegram(fail_doc_calls={2})   # variants.pdf arrives, answer_keys.pdf fails
    bot.session = tg
    ctx = fsm(dp, bot)
    await ctx.set_state(UploadStates.waiting_for_variant_count)
    await ctx.update_data(project_id=project_id)

    await dp.feed_update(bot, text_update("3"))
    assert await ctx.get_state() == UploadStates.waiting_for_variant_count.state
    assert tg.docs == ["variants.pdf"]
    await dp.feed_update(bot, text_update("3"))

    assert len(spy.deliveries) == 2
    stored = await _stored(env, project_id)
    for i, delivery in enumerate(spy.deliveries, start=1):
        gone = sorted(n for n, k in delivery["keys"].items() if (n, _j(k)) not in stored)
        assert not gone, (
            f"delivery {i}: the printed keys of variants {gone} are no longer stored — "
            "those papers would be graded against a key the teacher never received")

    from app.models.exam_edition import ExamEdition
    async with env.sm() as s:
        editions = (await s.execute(
            select(ExamEdition).where(ExamEdition.project_id == uuid.UUID(project_id))
            .order_by(ExamEdition.edition_number)
        )).scalars().all()
        assert [e.edition_number for e in editions] == [1, 2]
        assert [e.code for e in editions] == [d["code"] for d in spy.deliveries]
        for edition, delivery in zip(editions, spy.deliveries):
            rows = (await s.execute(
                select(Variant).where(Variant.edition_id == edition.id)
            )).scalars().all()
            assert {v.variant_number: v.answer_key for v in rows} == delivery["keys"]
            assert edition.source == "upload" and edition.variant_count == 3


# ── the database guarantee ───────────────────────────────────────────────────

@pytest.mark.parametrize("assignment", [
    "answer_key = '{\"1\": [\"Z\"]}'::jsonb",
    "question_order = '[]'::jsonb",
    "option_mapping = '{\"x\": {}}'::jsonb",
    "variant_number = variant_number + 100",
])
async def test_the_database_refuses_to_rewrite_a_stored_variant(sheet_env, assignment):
    project_id = await sheet_env.saved_project({1: {"1": ["A"], "2": ["B"]}})

    async with sheet_env.sm() as s:
        with pytest.raises(DBAPIError):
            await s.execute(text(f"UPDATE variants SET {assignment} WHERE project_id = :p"),
                            {"p": uuid.UUID(project_id)})
            await s.commit()

    assert await _stored(sheet_env, project_id) == {(1, _j({"1": ["A"], "2": ["B"]}))}


async def test_an_orm_key_change_is_refused_too(sheet_env):
    project_id = await sheet_env.saved_project({1: {"1": ["A"]}})
    async with sheet_env.sm() as s:
        row = (await s.execute(select(Variant).where(
            Variant.project_id == uuid.UUID(project_id)))).scalar_one()
        row.answer_key = {"1": ["B"]}
        with pytest.raises(DBAPIError):
            await s.commit()


async def test_unfrozen_columns_stay_writable(sheet_env):
    """The trigger freezes what grading reads, not the whole row."""
    project_id = await sheet_env.saved_project({1: {"1": ["A"]}})
    async with sheet_env.sm() as s:
        await s.execute(text("UPDATE variants SET pdf_path = 'x.pdf' WHERE project_id = :p"),
                        {"p": uuid.UUID(project_id)})
        await s.commit()


def test_no_code_path_assigns_a_variants_grading_columns():
    pattern = re.compile(r"\.(answer_key|question_order|option_mapping)\s*=[^=]|update\(Variant\b")
    hits = [
        f"{p.relative_to(APP)}:{i}"
        for p in APP.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert not hits, f"something rewrites a stored variant again: {hits}"


def test_all_three_writers_create_editions():
    from app.bot.handlers import book, multi_source, upload
    from app.services import variant_store

    assert not hasattr(variant_store, "upsert_variants")
    for mod in (upload, book, multi_source):
        src = inspect.getsource(mod)
        assert "create_edition" in src, f"{mod.__name__} does not create an edition"
        assert "upsert_variants" not in src


def test_uniqueness_is_per_edition_not_per_project():
    constraints = {
        tuple(sorted(c.name for c in con.columns))
        for con in Variant.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("edition_id", "variant_number") in constraints
    assert ("project_id", "variant_number") not in constraints


# ── create_edition ───────────────────────────────────────────────────────────

OWNER_TG = TELEGRAM_USER_ID_LIMIT + 520


async def _project(session) -> Project:
    owner = User(telegram_id=OWNER_TG, full_name="Edition Owner")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="Editions", status=ProjectStatus.COMPLETED)
    session.add(project)
    await session.flush()
    return project


def _variant(number: int, key: dict, questions=None) -> dict:
    v = {"variant_number": number, "question_order": [f"q{number}"],
         "option_mapping": {}, "answer_key": key}
    if questions is not None:
        v["questions_data"] = questions
    return v


async def test_a_second_edition_is_new_rows_and_the_first_is_untouched(session):
    from app.services.variant_store import create_edition

    project = await _project(session)
    first = await create_edition(session, project.id, [_variant(1, {"1": ["A"]}),
                                                       _variant(2, {"1": ["B"]})],
                                 source="upload")
    await session.flush()
    first_rows = {v.variant_number: (v.id, v.answer_key) for v in (await session.execute(
        select(Variant).where(Variant.edition_id == first.id))).scalars()}

    second = await create_edition(session, project.id, [_variant(1, {"1": ["C"]})],
                                  source="upload")
    await session.flush()

    assert (first.edition_number, second.edition_number) == (1, 2)
    assert first.code != second.code
    again = {v.variant_number: (v.id, v.answer_key) for v in (await session.execute(
        select(Variant).where(Variant.edition_id == first.id))).scalars()}
    assert again == first_rows
    assert second.variant_count == 1 and first.variant_count == 2


async def test_edition_codes_are_short_and_unambiguous(session):
    from app.services.variant_store import EDITION_CODE_ALPHABET, create_edition

    assert not set("0O1IL") & set(EDITION_CODE_ALPHABET)
    project = await _project(session)
    edition = await create_edition(session, project.id, [_variant(1, {"1": ["A"]})],
                                   source="book")
    assert len(edition.code) == 4 and set(edition.code) <= set(EDITION_CODE_ALPHABET)

    chosen = await create_edition(session, project.id, [_variant(1, {"1": ["A"]})],
                                  source="book", code="K7Q2")
    assert chosen.code == "K7Q2"


async def test_the_edition_snapshots_question_types_and_labels(session):
    """Grading must not depend on Question rows that can still be edited: the
    open positions and the offered labels are frozen with the edition."""
    from app.services.variant_store import create_edition

    project = await _project(session)
    questions = [
        {"position_in_variant": 1, "options": {"B": "x", "A": "y"}},
        {"position_in_variant": 2, "options": {}},
        {"position_in_variant": 3, "options": {"1": "one", "2": " "}},
    ]
    edition = await create_edition(
        session, project.id, [_variant(1, {"1": ["A"], "2": ["PHONE"], "3": ["1"]}, questions)],
        source="upload", title="Biologiya", pdf_format="compact",
    )
    await session.flush()
    row = (await session.execute(select(Variant).where(Variant.edition_id == edition.id))).scalar_one()

    assert row.open_positions == [2]
    assert edition.option_labels == ["1", "A", "B"]
    assert (edition.title, edition.pdf_format) == ("Biologiya", "compact")


async def test_variants_without_question_data_snapshot_nothing(session):
    from app.services.variant_store import create_edition

    project = await _project(session)
    edition = await create_edition(session, project.id, [_variant(1, {"1": ["A"]})],
                                   source="multi")
    await session.flush()
    row = (await session.execute(select(Variant).where(Variant.edition_id == edition.id))).scalar_one()
    assert row.open_positions is None and edition.option_labels is None


async def test_a_book_test_is_saved_as_edition_one(sheet_env):
    from app.bot.handlers import book
    from app.models.exam_edition import ExamEdition
    from app.services.variant_generator import generate_variants

    sheet_env.mp.setattr(book, "async_session_factory", sheet_env.sm)
    raw_qs = [
        {"question_id": str(uuid.uuid4()), "question_number": 1, "question_text": "Q1",
         "options": {"A": "a", "B": "b"}, "correct_answer": "A", "correct_answers": ["A"]},
        {"question_id": str(uuid.uuid4()), "question_number": 2, "question_text": "Q2",
         "options": {}, "correct_answer": None, "correct_answers": ["TOSHKENT"]},
    ]
    variants = generate_variants(raw_qs, 2)
    project_id = await book._persist_book_test(
        sheet_env.teacher.id, "Kitob", raw_qs, variants,
        edition_code="K7Q2", pdf_format="standard",
    )

    async with sheet_env.sm() as s:
        edition = (await s.execute(select(ExamEdition).where(
            ExamEdition.project_id == uuid.UUID(project_id)))).scalar_one()
        rows = (await s.execute(select(Variant).where(Variant.edition_id == edition.id))).scalars().all()
    assert (edition.edition_number, edition.code, edition.source) == (1, "K7Q2", "book")
    assert len(rows) == 2
    for row in rows:
        open_pos = row.question_order.index(raw_qs[1]["question_id"]) + 1
        assert row.open_positions == [open_pos]


# ── the printed code ─────────────────────────────────────────────────────────

def _pdf_text(pdf: bytes) -> str:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(stream=pdf, filetype="pdf")
    return " ".join(page.get_text() for page in doc)


def _printable(code=None) -> dict:
    v = {"variant_number": 3, "answer_key": {"1": ["A"]},
         "questions_data": [{"position_in_variant": 1, "question_text": "Savol",
                             "options": {"A": "bir", "B": "ikki"}}]}
    if code:
        v["edition_code"] = code
    return v


@pytest.mark.parametrize("builder", ["build_variants_pdf", "build_variants_pdf_compact"])
def test_the_paper_prints_the_edition_code_next_to_the_variant(builder):
    from app.services import pdf_generator

    printed = _pdf_text(getattr(pdf_generator, builder)([_printable("K7Q2")], "T"))
    assert "Variant 3 · K7Q2" in printed


def test_the_answer_key_prints_the_edition_code():
    from app.services.pdf_generator import build_answer_key_pdf

    assert "3-Variant · K7Q2" in _pdf_text(build_answer_key_pdf([_printable("K7Q2")], "T"))


def test_a_variant_without_a_code_prints_as_before():
    from app.services.pdf_generator import build_answer_key_pdf, build_variants_pdf

    assert "·" not in _pdf_text(build_variants_pdf([_printable()], "T"))
    assert "·" not in _pdf_text(build_answer_key_pdf([_printable()], "T"))
