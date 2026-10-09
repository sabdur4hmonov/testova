"""Per-user serialization of media-carrying messages.

AUDIT B-12. `Dispatcher(handle_as_tasks=True)` is aiogram 3.20's default and
`app/bot/main.py` does not override it, so N photos sent as ONE ALBUM become N
concurrent handler tasks for the SAME user. Each consumes CHECK quota, each
runs a Gemini read, and each overwrites the same FSM keys — `sheet_answers`,
`answer_sheet_key`, `student_name` — so the grade reported could belong to a
different photo than the name shown with it. `_append_run_result`
read-modify-writes `run_results` and loses entries, and in multi_source every
concurrent file computes `len(sources)+1` from the same snapshot, producing a
duplicate `file_index`.

WHY NOT THE TWO OBVIOUS FIXES:
  * `handle_as_tasks=False` serialises EVERY update for EVERY user globally.
    One long generation would then block the entire bot, trading a data race
    for an availability problem.
  * an album debounce (collect by media_group_id, handle once) is wrong here:
    each photo is a DIFFERENT student's answer sheet and every one of them must
    still be graded. The bug is the interleave, not the multiplicity.

So the lock is per USER and applies ONLY to messages carrying media. One
teacher's photos serialise; different teachers stay fully concurrent; and
callbacks and plain text are never held, so a teacher can still tap Cancel
while a sheet is being graded.

IN-MEMORY, per process — the same limitation ThrottlingMiddleware already has.
That covers the real case (one album arrives over one connection to one
process). If the bot is ever scaled to multiple processes, this needs Redis,
exactly like the throttler.

WHAT IT DID NOT FIX, AND WHO NO LONGER USES IT (CP-4). aiogram evaluates the
state filter BEFORE inner middlewares run (FSM state is loaded outside this
lock; events isolation is off by default), so a second photo that arrived
during the first one's read had already matched `waiting_for_answer_sheet`,
waited here, then ran anyway and overwrote the first sheet's FSM keys. The
checking flow now keeps every per-sheet fact on its own `sheet_checks` row, so
it has nothing shared left for this lock to protect — and the lock was
reading a whole class's sheets one at a time. Its photo handlers opt out with
`flags={"media_lock": False}`. Other media flows (multi_source's file_index,
book uploads) still take the lock.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import TelegramObject

from app.utils.logging import get_logger

logger = get_logger(__name__)


class MediaSerializationMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if get_flag(data, "media_lock") is False:
            return await handler(event, data)   # opted out (see module docstring)

        has_media = bool(
            getattr(event, "photo", None) or getattr(event, "document", None)
        )
        user = getattr(event, "from_user", None)
        user_id = getattr(user, "id", None)

        if not has_media or user_id is None:
            return await handler(event, data)

        lock = self._lock_for(user_id)
        if lock.locked():
            # Not an error — this is the album case doing exactly what it should.
            logger.debug("media_serialized", user_id=user_id)
        # `async with` releases on EVERY exit path, including an exception: a
        # leaked lock would wedge that teacher's uploads for the process's
        # lifetime, which is worse than the race it fixes.
        async with lock:
            return await handler(event, data)
