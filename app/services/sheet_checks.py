"""Persistence for sheet_checks rows — one answer-sheet photo each (CP-4).

Every function takes the session factory explicitly, the same shape as
app.services.quota, so a handler passes its own `async_session_factory` and a
test that patches the handler's factory patches this too.

THE ONE RULE: a status change is a single conditional UPDATE
(`WHERE id = … AND status IN expect [AND confirm_pending = …] RETURNING *`).
Postgres applies it atomically, so when two inputs race for the same sheet —
a double tap, two typed names, a stale button — exactly one of them gets the
row back and the other gets None and does nothing.
"""
from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from app.models.sheet_check import SheetCheck

READING = "reading"
WORKING = "working"
UNREADABLE = "unreadable"
INCOMPLETE = "incomplete"
NEEDS_NAME = "needs_name"
NEEDS_VARIANT = "needs_variant"
NEEDS_CONFIRM = "needs_confirm"
GRADED = "graded"
FAILED = "failed"

WAITING = (NEEDS_NAME, NEEDS_VARIANT, NEEDS_CONFIRM)


def _as_uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def create(session_factory, *, user_id: int, run_id, flow: str, **fields) -> SheetCheck:
    """A new sheet in `reading` — created before the Gemini call, so a read
    that raises still leaves a record of the photo."""
    async with session_factory() as session:
        row = SheetCheck(user_id=user_id, run_id=_as_uuid(run_id), flow=flow,
                         status=READING, **fields)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def get(session_factory, check_id, *, user_id: int) -> SheetCheck | None:
    async with session_factory() as session:
        return (await session.execute(
            select(SheetCheck).where(SheetCheck.id == _as_uuid(check_id),
                                     SheetCheck.user_id == user_id)
        )).scalar_one_or_none()


def _transition_stmt(check_id, user_id: int, expect, pending, fields: dict):
    expected = (expect,) if isinstance(expect, str) else tuple(expect)
    stmt = (
        update(SheetCheck)
        .where(SheetCheck.id == _as_uuid(check_id),
               SheetCheck.user_id == user_id,
               SheetCheck.status.in_(expected))
    )
    if pending is not None:
        stmt = stmt.where(SheetCheck.confirm_pending == literal(list(pending), type_=JSONB))
    return (
        stmt.values(updated_at=func.now(), **fields)
        .returning(SheetCheck)
        .execution_options(synchronize_session=False)
    )


async def transition(
    session_factory, check_id, *, user_id: int, expect: str | Iterable[str],
    pending: list | None = None, **fields,
) -> SheetCheck | None:
    """Apply `fields` only if the sheet is in one of `expect` (and, when given,
    its confirm_pending still equals `pending`). Returns the updated row, or
    None when another input got there first."""
    stmt = _transition_stmt(check_id, user_id, expect, pending, fields)
    async with session_factory() as session:
        row = (await session.execute(stmt)).scalar_one_or_none()
        await session.commit()
        return row


async def grade(
    session_factory, check_id, *, user_id: int, rows: Iterable = (), **fields,
) -> SheetCheck | None:
    """Move a `working` sheet to `graded` AND add its result rows (Submission,
    check_results) in ONE transaction (AUDIT F06).

    The conditional UPDATE runs first: if another input already took the sheet
    it returns None, the transaction is rolled back and no row is written — so
    a sheet's result rows exist exactly once, and only together with its
    `graded` status. The caller sends the report AFTER this commits, so a
    failed send can no longer lose a grade."""
    stmt = _transition_stmt(check_id, user_id, WORKING, None,
                            {"status": GRADED, **fields})
    async with session_factory() as session:
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            await session.rollback()
            return None
        session.add_all(list(rows))
        await session.commit()
        return row


async def waiting(
    session_factory, *, user_id: int, run_id, statuses: Iterable[str] = WAITING,
) -> list[SheetCheck]:
    """This run's sheets in `statuses`, oldest first."""
    async with session_factory() as session:
        return list((await session.execute(
            select(SheetCheck)
            .where(SheetCheck.user_id == user_id,
                   SheetCheck.run_id == _as_uuid(run_id),
                   SheetCheck.status.in_(tuple(statuses)))
            .order_by(SheetCheck.created_at, SheetCheck.id)
        )).scalars().all())


async def in_run(session_factory, *, user_id: int, run_id) -> list[SheetCheck]:
    """Every sheet of a run, whatever its status, oldest first."""
    async with session_factory() as session:
        return list((await session.execute(
            select(SheetCheck)
            .where(SheetCheck.user_id == user_id, SheetCheck.run_id == _as_uuid(run_id))
            .order_by(SheetCheck.created_at, SheetCheck.id)
        )).scalars().all())


async def find_by_message(
    session_factory, *, user_id: int, message_id: int,
) -> SheetCheck | None:
    """The teacher's sheet a message belongs to — the bot prompt that asked its
    question, or the student's photo itself. Newest first if ids ever repeat.
    Deliberately NOT scoped to a run: the caller must be able to tell "this
    belongs to an earlier check" apart from "this is not a sheet message"."""
    async with session_factory() as session:
        return (await session.execute(
            select(SheetCheck)
            .where(SheetCheck.user_id == user_id,
                   (SheetCheck.prompt_message_id == message_id)
                   | (SheetCheck.source_message_id == message_id))
            .order_by(SheetCheck.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()


async def oldest_waiting(
    session_factory, *, user_id: int, run_id, statuses: Iterable[str],
) -> SheetCheck | None:
    rows = await waiting(session_factory, user_id=user_id, run_id=run_id, statuses=statuses)
    return rows[0] if rows else None
