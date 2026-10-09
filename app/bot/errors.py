"""Global unhandled-exception handler.

AUDIT B-8. No `dp.errors` handler was registered anywhere, so an exception
escaping any handler was logged by aiogram and NOTHING was sent to the user.
On the generation path that meant: the BOOK_GENERATION quota unit already
consumed, the "⏳ Savollar tayyorlanmoqda" heartbeat still on screen, the FSM
state never moving, and the teacher waiting forever with no idea anything had
failed.

This is deliberately ONE global handler rather than a try/except added to each
of the eight handlers the sweep found unprotected. The audit itself calls the
missing handler "the amplifier for every other unhandled failure in the bot" —
B-11's HTML 400s, B-27's Redis timeouts, unguarded `uuid.UUID()` parses — so
fixing it once covers the paths nobody has audited yet, including handlers that
do not exist today.

The shape copies `multi_source._generate_from_pool`, which already did this
correctly: a short code in the teacher's message, the full traceback in the log
under the same code, so a screenshot can be traced to a log line.

It does NOT clear FSM state or refund quota. Both are tempting and both are
wrong here: this handler cannot know which flow raised or how far it got, and
guessing would strand a teacher mid-flow or hand back a unit that was really
spent on a real Gemini call.
"""
from __future__ import annotations

import traceback
import uuid

from app.utils.logging import get_logger

logger = get_logger(__name__)

_SORRY = {
    "uz": "❌ Kutilmagan xatolik yuz berdi. Qayta urinib ko'ring.\nXatolik kodi: {code}",
    "en": "❌ Something went wrong. Please try again.\nError code: {code}",
    "ru": "❌ Произошла непредвиденная ошибка. Попробуйте ещё раз.\nКод ошибки: {code}",
}


def _text(code: str, lang: str = "uz") -> str:
    return _SORRY.get(lang, _SORRY["uz"]).format(code=f"#{code}")


async def on_unhandled_error(event) -> bool:
    """aiogram `dp.errors` handler. Returns True so the update counts handled.

    Every step is defensive: this is the LAST line of defence, and if it raises
    the process logs twice and the user still gets nothing.
    """
    code = uuid.uuid4().hex[:6].upper()
    exc = getattr(event, "exception", None)

    try:
        logger.error(
            "unhandled_error",
            code=code,
            error=str(exc),
            error_type=type(exc).__name__ if exc else "unknown",
            traceback="".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ) if exc is not None else "",
        )
    except Exception:  # noqa: BLE001
        pass

    # Reach the user through whichever half of the update we have.
    try:
        update = getattr(event, "update", None)
        message = getattr(update, "message", None)
        callback = getattr(update, "callback_query", None)

        lang = "uz"
        try:
            user = getattr(message or callback, "from_user", None)
            lang = getattr(user, "language_code", None) or "uz"
            if lang not in _SORRY:
                lang = "uz"
        except Exception:  # noqa: BLE001
            pass

        if message is not None:
            await message.answer(_text(code, lang))
        elif callback is not None:
            try:
                await callback.answer()
            except Exception:  # noqa: BLE001
                pass
            if getattr(callback, "message", None) is not None:
                await callback.message.answer(_text(code, lang))
    except Exception:  # noqa: BLE001 — never let the safety net itself raise
        logger.warning("unhandled_error_notify_failed", code=code)

    return True
