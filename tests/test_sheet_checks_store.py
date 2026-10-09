"""
app.services.sheet_checks — every status change is one atomic conditional
UPDATE, so two inputs racing for the same sheet can never both take it.

Real per-run database with COMMITTING sessions: the race test runs two claims
on two connections through asyncio.gather, which one shared savepoint
connection would serialise and so could not fail.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401
from app.models.sheet_check import SheetCheck
from app.services import sheet_checks as S
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT

TG = TELEGRAM_USER_ID_LIMIT + 70
OTHER_TG = TELEGRAM_USER_ID_LIMIT + 71


@pytest_asyncio.fixture
async def sm(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean():
        async with factory() as s:
            await s.execute(delete(SheetCheck).where(SheetCheck.user_id.in_([TG, OTHER_TG])))
            await s.commit()
    await _clean()
    try:
        yield factory
    finally:
        await _clean()


async def test_a_new_sheet_starts_reading(sm):
    run = uuid.uuid4()
    row = await S.create(sm, user_id=TG, run_id=run, flow="saved", file_id="F1")
    assert row.status == S.READING
    assert row.open_questions == [] and row.confirm_pending == [] and row.confirm_overrides == {}
    assert (await S.get(sm, row.id, user_id=TG)).file_id == "F1"
    assert await S.get(sm, row.id, user_id=OTHER_TG) is None, "a sheet leaked across teachers"


async def test_a_transition_only_applies_from_the_expected_status(sm):
    row = await S.create(sm, user_id=TG, run_id=uuid.uuid4(), flow="saved")
    assert await S.transition(sm, row.id, user_id=TG, expect=S.NEEDS_NAME,
                              status=S.WORKING) is None
    moved = await S.transition(sm, row.id, user_id=TG, expect=S.READING,
                               status=S.NEEDS_NAME, student_name="Ali")
    assert moved.status == S.NEEDS_NAME and moved.student_name == "Ali"


async def test_two_racing_claims_on_one_sheet_exactly_one_wins(sm):
    row = await S.create(sm, user_id=TG, run_id=uuid.uuid4(), flow="saved")
    await S.transition(sm, row.id, user_id=TG, expect=S.READING, status=S.NEEDS_NAME)

    results = await asyncio.gather(*(
        S.transition(sm, row.id, user_id=TG, expect=S.NEEDS_NAME,
                     status=S.WORKING, student_name=name)
        for name in ("Ali", "Vali")
    ))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"{len(winners)} claims took the same sheet"
    assert (await S.get(sm, row.id, user_id=TG)).student_name == winners[0].student_name


async def test_a_stale_confirm_queue_cannot_advance_the_sheet(sm):
    row = await S.create(sm, user_id=TG, run_id=uuid.uuid4(), flow="manual")
    await S.transition(sm, row.id, user_id=TG, expect=S.READING,
                       status=S.NEEDS_CONFIRM, confirm_pending=[5, 22])
    # First tap on q5 advances the queue.
    first = await S.transition(sm, row.id, user_id=TG, expect=S.NEEDS_CONFIRM,
                               pending=[5, 22], status=S.NEEDS_CONFIRM, confirm_pending=[22])
    assert first.confirm_pending == [22]
    # A double tap still carrying the old queue gets nothing.
    assert await S.transition(sm, row.id, user_id=TG, expect=S.NEEDS_CONFIRM,
                              pending=[5, 22], status=S.WORKING, confirm_pending=[22]) is None


async def test_waiting_is_scoped_to_the_run_and_ordered_oldest_first(sm):
    run, other_run = uuid.uuid4(), uuid.uuid4()
    first = await S.create(sm, user_id=TG, run_id=run, flow="saved", file_id="A")
    second = await S.create(sm, user_id=TG, run_id=run, flow="saved", file_id="B")
    elsewhere = await S.create(sm, user_id=TG, run_id=other_run, flow="saved", file_id="C")
    foreign = await S.create(sm, user_id=OTHER_TG, run_id=run, flow="saved", file_id="D")
    for r, uid in ((first, TG), (second, TG), (elsewhere, TG), (foreign, OTHER_TG)):
        await S.transition(sm, r.id, user_id=uid, expect=S.READING, status=S.NEEDS_NAME)

    got = await S.waiting(sm, user_id=TG, run_id=run, statuses=(S.NEEDS_NAME,))
    assert [r.file_id for r in got] == ["A", "B"]
    oldest = await S.oldest_waiting(sm, user_id=TG, run_id=run, statuses=(S.NEEDS_NAME,))
    assert oldest.file_id == "A"
    assert await S.oldest_waiting(sm, user_id=TG, run_id=run, statuses=(S.NEEDS_VARIANT,)) is None
