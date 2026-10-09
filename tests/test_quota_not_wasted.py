"""AUDIT B-7 / B-8 / C-14 - stop paying for nothing.

Three ways a teacher's quota was spent for no delivered result:

  B-7  a second tap on the generation button while the first was still running
       started a SECOND paid generation (the state filter still matched,
       because _generate_one_attempt only changes state at the very end).
  B-8  an exception inside _run_generation left the unit consumed, the status
       message orphaned and the user with NO message at all.
  C-14 the CHECK unit was consumed BEFORE the download and before the
       "project has no variants" bail - both of which return having made zero
       Gemini calls.

No refund mechanism exists anywhere in this codebase (verified by grep), so
these are fixed by NOT consuming, or by surfacing the failure - never by
inventing a rollback.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.bot.handlers import book, checking
from app.services import quota


# -- fakes (same shape as test_book_edit_and_photos.py) ----------------------

class _State:
    def __init__(self, **data):
        self.state = None
        self.data: dict = dict(data)
        self.cleared = False

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data = {}
        self.cleared = True


class _Msg:
    def __init__(self, photo=None, document=None, caption=None):
        self.photo = photo
        self.document = document
        self.caption = caption
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.deleted = False

    async def answer(self, text, **kw):
        self.answers.append(text)
        return _Msg()

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class _CB:
    def __init__(self, data, message):
        self.data = data
        self.message = message

    async def answer(self, *a, **k):
        return None


def _user(tg=4242):
    return SimpleNamespace(id="uid", telegram_id=tg,
                           language=SimpleNamespace(value="uz"))


def _gen_state():
    """Minimum FSM data _generate_one_attempt needs to reach the quota gate."""
    return _State(
        gen_theme_titles=["T1"], gen_theme_texts=["some source text"],
        gen_total=10, gen_book_id=None, gen_theme_ids=[],
    )


async def _ok_generation(*a, **kw):
    # Four lists: aggregated, errored, thin-short, filtered-short.
    return ([], [], [], [])


async def _noop(*a, **kw):
    return None


@pytest.fixture
def spy_quota(monkeypatch):
    """Count shared-pool (quota.TEST) consumptions; always allow."""
    calls = []

    async def fake(session_factory, tg, kind):
        calls.append(kind)
        return True

    monkeypatch.setattr(book.quota, "check_and_consume", fake)
    return calls


# -- B-7: double tap --------------------------------------------------------

async def test_double_tap_generation_is_billed_once(monkeypatch, spy_quota):
    """Two taps on the same button while the first generation is in flight."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_generation(*a, **kw):
        started.set()
        await release.wait()          # hold the first attempt open
        return ([], [], [], [])

    monkeypatch.setattr(book, "_run_generation", slow_generation)
    monkeypatch.setattr(book, "_show_generation_review", _noop)

    state = _gen_state()
    user = _user()

    # BOTH taps run as tasks: with the bug present the second one also enters
    # _run_generation and blocks on `release`, so awaiting it inline would
    # deadlock the test instead of failing it.
    first = asyncio.create_task(
        book.handle_book_images_choice(_CB("bookimg:no", _Msg()), state, user))
    await asyncio.wait_for(started.wait(), timeout=5)
    second = asyncio.create_task(
        book.handle_book_images_choice(_CB("bookimg:no", _Msg()), state, user))
    await asyncio.sleep(0.05)         # let the second tap reach the guard
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert spy_quota == [quota.TEST], (
        f"double tap billed {len(spy_quota)} units, expected 1")


# -- B-8: a mid-generation exception -----------------------------------------

async def test_generation_failure_tells_the_user(monkeypatch, spy_quota):
    """A raise inside _run_generation must not leave the teacher staring at a
    status message that never resolves."""
    async def boom(*a, **kw):
        raise RuntimeError("gemini exploded")

    monkeypatch.setattr(book, "_run_generation", boom)
    monkeypatch.setattr(book, "_show_generation_review", _noop)

    state = _gen_state()
    msg = _Msg()
    await book.handle_book_images_choice(_CB("bookimg:no", msg), state, _user())

    assert msg.answers, "user got no message at all after the failure"
    assert state.cleared, "FSM state left dangling after the failure"


# -- B-7 x B-8: the interaction the finally/except must survive --------------

async def test_double_tap_where_the_attempt_also_fails(monkeypatch, spy_quota):
    """The dangerous combination: a double tap AND the (single) attempt fails.

    The in-flight guard must still be released, or the teacher is locked out of
    ever retrying - a worse bug than the double billing it prevents.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_boom(*a, **kw):
        started.set()
        await release.wait()
        raise RuntimeError("gemini exploded mid-attempt")

    monkeypatch.setattr(book, "_run_generation", slow_boom)
    monkeypatch.setattr(book, "_show_generation_review", _noop)

    state = _gen_state()
    user = _user()
    first = asyncio.create_task(
        book.handle_book_images_choice(_CB("bookimg:no", _Msg()), state, user))
    await asyncio.wait_for(started.wait(), timeout=5)
    second = asyncio.create_task(
        book.handle_book_images_choice(_CB("bookimg:no", _Msg()), state, user))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), timeout=5)

    assert spy_quota == [quota.TEST], (
        f"double tap billed {len(spy_quota)} units, expected 1")

    # ...and the guard must be gone, so a LATER attempt still works.
    monkeypatch.setattr(book, "_run_generation", _ok_generation)
    await book.handle_book_images_choice(
        _CB("bookimg:no", _Msg()), _gen_state(), user)
    assert len(spy_quota) == 2, "in-flight guard was never released after failure"


# -- C-14: CHECK consumed before validation ---------------------------------

@pytest.fixture
def spy_check(monkeypatch):
    calls = []

    async def fake(session_factory, tg, kind):
        calls.append(kind)
        return True

    async def _allow(*a, **kw):
        return True

    monkeypatch.setattr(checking.quota, "check_and_consume", fake)
    # The PEEK and the token-budget backstop read the users table too. Unpatched,
    # they queried the LIVE database and their fail-open `except` hid it; the
    # connection guard (tests/live_db_guard.py) is what surfaced it. A test that
    # needs a refusal re-patches check_available after this fixture.
    monkeypatch.setattr(checking.quota, "check_available", _allow)
    monkeypatch.setattr(checking.quota, "within_token_budget", _allow)
    return calls


def _photo_msg():
    return _Msg(photo=[SimpleNamespace(file_id="f1", file_size=1000)])


async def test_check_not_billed_when_project_has_no_variants(monkeypatch, spy_check):
    """Nothing to grade against - zero Gemini calls, so zero charge."""
    async def _dl(*a, **kw):
        return b"jpegbytes"

    async def _save(*a, **kw):
        return "key"

    async def _variants(*a, **kw):
        return ({}, 0)

    monkeypatch.setattr(checking, "fetch_telegram_file", _dl)
    monkeypatch.setattr(checking.storage, "save_file", _save)
    monkeypatch.setattr(checking, "_project_variants", _variants)

    await checking.handle_answer_sheet_upload(
        _photo_msg(), _State(project_id="p1"), _user(), bot=SimpleNamespace())

    assert spy_check == [], "billed for a project with no variants"


async def test_check_not_billed_when_the_download_fails(monkeypatch, spy_check):
    async def _dl(*a, **kw):
        return None

    monkeypatch.setattr(checking, "fetch_telegram_file", _dl)

    await checking.handle_answer_sheet_upload(
        _photo_msg(), _State(project_id="p1"), _user(), bot=SimpleNamespace())

    assert spy_check == [], "billed for a sheet that never downloaded"


# -- guard against over-correcting: the happy paths must still bill ----------

async def test_single_generation_still_bills_once(monkeypatch, spy_quota):
    monkeypatch.setattr(book, "_run_generation", _ok_generation)
    monkeypatch.setattr(book, "_show_generation_review", _noop)

    await book.handle_book_images_choice(
        _CB("bookimg:no", _Msg()), _gen_state(), _user())

    assert spy_quota == [quota.TEST]


async def test_check_is_billed_once_on_a_real_read(monkeypatch, spy_check):
    """The read happened, so the unit is correctly spent."""
    async def _dl(*a, **kw):
        return b"jpegbytes"

    async def _save(*a, **kw):
        return "key"

    async def _variants(*a, **kw):
        return ({1: {"1": "A"}}, 1)

    async def _labels(*a, **kw):
        return ["A", "B", "C", "D"]

    async def _read(*a, **kw):
        raise RuntimeError("read blew up after the call was already paid for")

    # CP-4: the handler records the photo as a sheet_checks row before the paid
    # read. This file has no database fixture, so the store is faked in memory —
    # never let it fall through to the app's real session factory (the LIVE db).
    sheets: list[dict] = []

    async def _create(session_factory, **fields):
        row = SimpleNamespace(id="sheet-1", user_id=fields["user_id"], status="reading")
        sheets.append({"row": row, **fields})
        return row

    async def _transition(session_factory, check_id, *, user_id, expect, **fields):
        row = sheets[0]["row"]
        row.status = fields.get("status", row.status)
        return row

    monkeypatch.setattr(checking.sheet_checks, "create", _create)
    monkeypatch.setattr(checking.sheet_checks, "transition", _transition)
    monkeypatch.setattr(checking, "fetch_telegram_file", _dl)
    monkeypatch.setattr(checking.storage, "save_file", _save)
    monkeypatch.setattr(checking, "_project_variants", _variants)
    monkeypatch.setattr(checking, "_project_option_labels", _labels)
    monkeypatch.setattr(checking, "read_answer_sheet", _read)

    with pytest.raises(RuntimeError):
        await checking.handle_answer_sheet_upload(
            _photo_msg(), _State(project_id="p1"), _user(), bot=SimpleNamespace())

    assert spy_check == ["check"], (
        "a failure AFTER the Gemini call must still be charged - the call was real")


async def test_over_quota_is_rejected_before_any_download(monkeypatch, spy_check):
    """The other half of C-14, and a regression guard.

    Moving the CONSUME later must not move the REJECTION later: an over-quota
    teacher has to be turned away before the Telegram download and before any
    DB work, exactly as when the gate consumed up front. Getting this wrong
    made every over-quota submission pay a download + a storage write + a
    project query before saying "no" - and, in the test suite, opened pooled
    asyncpg connections on paths that used to return early, which surfaced as
    an "Event loop is closed" failure two files away.
    """
    downloaded = []

    async def _dl(*a, **kw):
        downloaded.append(1)
        return b"jpegbytes"

    async def _no_quota(session_factory, tg, kind):
        return False

    monkeypatch.setattr(checking, "fetch_telegram_file", _dl)
    monkeypatch.setattr(checking.quota, "check_available", _no_quota)

    msg = _photo_msg()
    await checking.handle_answer_sheet_upload(
        msg, _State(project_id="p1"), _user(), bot=SimpleNamespace())

    assert downloaded == [], "over-quota user paid for a download before rejection"
    assert spy_check == [], "over-quota user must not be charged either"
    assert msg.answers, "over-quota user got no explanation"
