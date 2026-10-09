"""AUDIT B-12 (album FSM race) and B-13 (garbled OCR text layer trusted).

B-12: `Dispatcher(handle_as_tasks=True)` is aiogram 3.20's default and
`app/bot/main.py` does not override it, so N photos sent as ONE ALBUM become N
concurrent handler tasks for the same user. Each consumes CHECK quota, each
runs a Gemini read, and each overwrites the same FSM keys
(`sheet_answers` / `answer_sheet_key` / `student_name`) — so **the grade
reported can belong to a different photo than the name**. `_append_run_result`
read-modify-writes `run_results` and loses entries; in multi_source every
concurrent file computes `len(sources)+1` from the same snapshot and produces
a duplicate `file_index`.

The fix is NOT `handle_as_tasks=False` (that serialises every user's every
update globally — one long generation would block the whole bot) and NOT an
album debounce (each photo is a DIFFERENT student's sheet and must still be
graded). It is a per-user lock applied ONLY to media-carrying messages, so:
  * one teacher's photos serialise, ending the interleave;
  * different teachers stay fully concurrent;
  * button taps stay responsive during a long upload, because callbacks are
    never held by this lock.

B-13: `has_text_layer` counts CHARACTERS only, so a scan whose text layer was
baked by someone else's OCR passes, the garble is saved `status='ready'` (the
terminal state — never redone, never re-billed) and every generated question is
grounded in nonsense.

CALIBRATION AND ITS LIMIT, stated rather than implied: every text-carrying page
of all six real books in storage was measured — **657 pages, zero false
positives**, with the worst real page at vowel_ratio 0.337 (threshold fires
below 0.25) and novowel_ratio 0.024 (fires above 0.30). But NO REAL GARBLED
SAMPLE EXISTS in the current corpus — the Grammarway garble recorded earlier is
not present in the copy now stored — so what the detector CATCHES is calibrated
against synthetic OCR garble, not real. It therefore sits far from real content
rather than close to garble: it reliably catches severe garble and deliberately
lets mild garble through, because a false positive spends real money on an
unnecessary Vision pass while a false negative merely preserves today's
behaviour. Revisit when a genuinely garbled book turns up.
"""
from __future__ import annotations

import asyncio

import pytest

from app.bot.middlewares.media_lock import MediaSerializationMiddleware
from app.services.book_extractor import (
    GARBLE_MAX_NOVOWEL_RATIO, GARBLE_MIN_VOWEL_RATIO, text_readability,
)

# ── B-12 ───────────────────────────────────────────────────────────────────


class _Msg:
    def __init__(self, user_id: int, *, photo=True):
        self.photo = ["p"] if photo else None
        self.document = None
        self.from_user = type("U", (), {"id": user_id})()


async def _run(mw, msg, order: list, tag: str, delay: float = 0.02):
    async def handler(_event, _data):
        order.append(f"{tag}:start")
        await asyncio.sleep(delay)
        order.append(f"{tag}:end")

    return await mw(handler, msg, {})


async def test_two_photos_from_one_user_do_not_interleave():
    """THE bug: two album photos ran concurrently and overwrote each other's
    FSM keys, so a grade could be reported under another photo's name."""
    mw = MediaSerializationMiddleware()
    order: list[str] = []

    await asyncio.gather(
        _run(mw, _Msg(1), order, "a"),
        _run(mw, _Msg(1), order, "b"),
    )

    assert order in (
        ["a:start", "a:end", "b:start", "b:end"],
        ["b:start", "b:end", "a:start", "a:end"],
    ), f"handlers interleaved: {order}"


async def test_photos_from_different_users_stay_concurrent():
    """The lock must not serialise the whole bot — two teachers grading at once
    is the normal case."""
    mw = MediaSerializationMiddleware()
    order: list[str] = []

    await asyncio.gather(
        _run(mw, _Msg(1), order, "a"),
        _run(mw, _Msg(2), order, "b"),
    )

    assert order[0].endswith(":start") and order[1].endswith(":start"), (
        f"different users were serialised: {order}")


async def test_a_message_without_media_is_not_locked():
    """Button taps and typed text must stay responsive while a photo is being
    graded, or a teacher cannot cancel."""
    mw = MediaSerializationMiddleware()
    order: list[str] = []

    await asyncio.gather(
        _run(mw, _Msg(1), order, "photo", delay=0.05),
        _run(mw, _Msg(1, photo=False), order, "text", delay=0.0),
    )

    assert order.index("text:end") < order.index("photo:end"), (
        f"a plain message waited behind a photo: {order}")


async def test_the_lock_is_released_when_the_handler_raises():
    """A leaked lock would wedge that teacher's uploads for the whole process
    lifetime — worse than the race it fixes."""
    mw = MediaSerializationMiddleware()

    async def boom(_e, _d):
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError):
        await mw(boom, _Msg(1), {})

    order: list[str] = []
    await _run(mw, _Msg(1), order, "after")
    assert order == ["after:start", "after:end"]


async def test_the_handlers_result_is_returned_unchanged():
    mw = MediaSerializationMiddleware()

    async def handler(_e, _d):
        return "sentinel"

    assert await mw(handler, _Msg(1), {}) == "sentinel"


def test_the_middleware_is_registered_on_the_dispatcher():
    import inspect

    from app.bot import main as bot_main

    src = inspect.getsource(bot_main.create_dispatcher)
    assert "MediaSerializationMiddleware" in src


# ── B-13 ───────────────────────────────────────────────────────────────────

REAL_EN = (
    "We form the past continuous with was or were and the main verb with -ing. "
    "It is used for an action which was in progress at a stated past time. "
    "Complete the sentences below using the correct form of each verb given in "
    "brackets, and then check your answers with a partner before the lesson ends."
)
REAL_UZ = (
    "Kimyo fani moddalarning tuzilishi, xossalari va ularning o'zgarishlarini "
    "o'rganadigan tabiiy fanlardan biridir. Jism deb moddadan tashkil topgan "
    "har qanday narsaga aytiladi. Quyidagi savollarga javob bering va "
    "daftaringizga yozib qo'ying, so'ngra o'qituvchingizga ko'rsating."
)
REAL_RU = (
    "Химия изучает состав, строение и свойства веществ, а также их превращения. "
    "Ответьте на вопросы, запишите результаты наблюдений в тетрадь и сравните "
    "их с выводами, которые сделали ваши одноклассники на прошлом уроке."
)


@pytest.mark.parametrize("text", [REAL_EN, REAL_UZ, REAL_RU])
def test_real_text_is_never_called_garbled(text):
    """The property that actually matters: a false positive spends real money
    on an unnecessary Vision pass."""
    assert text_readability(text)["garbled"] is False


def test_severe_ocr_garble_is_caught():
    garbled = (
        "Wl flxd thc glsh c! rn!jlous fitm wns ov werI !dd xhe mair vrb c1tk "
        "-m nc lw sd lgec hok tht bnt kj vbn mkl trw qsd fgh jkl zxc vbn mqw "
        "prt lkj hgf dsz xcv bnm qwr tpl kjh gfd szx cvb nmq wrt plk jhg fds "
        "zxc vbn mkl trw qsd fgh jkl zxc vbn mqw prt lkj hgf dsz xcv bnm qwrt"
    )

    assert text_readability(garbled)["garbled"] is True


def test_a_short_snippet_is_never_judged():
    """Under the character floor the ratios are noise, so the detector must
    abstain rather than guess — abstaining costs nothing, guessing costs a
    Vision call."""
    assert text_readability("abc def")["garbled"] is False
    assert text_readability("")["garbled"] is False


def test_the_thresholds_keep_their_measured_margin():
    """Pins the calibration: the worst REAL page measured 0.337 vowel and 0.024
    novowel across 657 pages. If someone tightens these toward real content,
    this fails and they have to re-measure."""
    assert GARBLE_MIN_VOWEL_RATIO <= 0.27, "too close to the real floor of 0.337"
    assert GARBLE_MAX_NOVOWEL_RATIO >= 0.10, "too close to the real ceiling of 0.024"


def test_a_garbled_text_layer_routes_to_vision(monkeypatch):
    """End to end through the real decision: a PRESENT but unreadable text
    layer must fall through to Vision instead of being saved as ready."""
    from app.services import book_extractor as be

    monkeypatch.setattr(be, "has_text_layer", lambda *_a, **_k: True)
    monkeypatch.setattr(be, "extract_theme_text", lambda *_a, **_k: "qwrtplkjh " * 60)
    called = {}

    def _vision(pdf, s, e, user_id=None):
        called["vision"] = True
        return "clean transcription from the rendered page"

    monkeypatch.setattr(be, "extract_theme_text_vision", _vision)

    out = be.extract_theme(b"pdf", 1, 3)

    assert called.get("vision") is True
    assert out == "clean transcription from the rendered page"


def test_a_clean_text_layer_still_costs_nothing(monkeypatch):
    """The regression guard: a digital textbook must NOT start paying for
    Vision. That is what keeps a 400-page book affordable."""
    from app.services import book_extractor as be

    monkeypatch.setattr(be, "has_text_layer", lambda *_a, **_k: True)
    monkeypatch.setattr(be, "extract_theme_text", lambda *_a, **_k: REAL_EN * 3)

    def _vision(*_a, **_k):
        pytest.fail("Vision was called for a clean text layer")

    monkeypatch.setattr(be, "extract_theme_text_vision", _vision)

    assert be.extract_theme(b"pdf", 1, 3).startswith("We form")
