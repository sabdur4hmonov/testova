"""LIVE BUG 2 — a 17.6 MB Telegram download failed with a 102-second timeout.

MEASURED against the shipped formula (`6 s/MB`, 60 s floor):

    size    budget   implied minimum sustained throughput
     1 MB     60 s     17 KB/s
     5 MB     60 s     85 KB/s
    10 MB     60 s    171 KB/s
    17 MB    102 s    171 KB/s      <-- the reported failure, exactly
    20 MB    120 s    171 KB/s

So EVERY file of 10 MB or more demanded a sustained **171 KB/s**, and the
reported 102 s matches a ~17 MB file to the second. The budget was not
generous; it was pinned to an optimistic throughput floor that a phone on
mobile data does not reach. The module's own comment assumed "~170 KB/s
worst-case sustained", which is roughly a *good* mobile connection, not a
worst case.

SWEPT for the B-15 gap: `fetch_telegram_file` has **zero retry**, exactly like
`gemini_rest.call` did. All seven call sites go through this one helper, so a
transient blip — a reset connection, a DNS hiccup — costs the teacher a full
manual re-upload of a 17 MB file.

THE FIX IS BOTH, because they address different failure modes:
  * raising the budget alone does nothing for a connection that drops at
    3 seconds;
  * retrying alone is actively worse when the link is simply too slow — both
    attempts burn the full budget and the teacher waits twice as long for a
    guaranteed failure.

The retry is therefore CONDITIONAL: it fires only when the first attempt failed
FAST (well inside its budget), which is the signature of a transient blip. A
full-budget timeout means the link cannot carry the file, and retrying it is
just charging the teacher another five minutes for the same answer.
"""
from __future__ import annotations

import pytest

from app.bot import file_download as fd


async def _no_sleep(_seconds):
    """A real no-op coroutine.

    NOT `lambda s: asyncio.sleep(0)`: monkeypatching `fd.asyncio.sleep` patches
    the SHARED asyncio module, so such a lambda calls the patched function and
    recurses until the stack blows.
    """
    return None


# ── the budget ─────────────────────────────────────────────────────────────

def test_the_reported_file_now_gets_a_realistic_budget():
    """17.6 MB timed out at ~102 s. The new budget must assume a mobile-data
    floor, not a good-connection one."""
    budget = fd.download_timeout_seconds(int(17.6 * 1024 * 1024))

    assert budget > 102, f"still {budget}s — the reported failure would repeat"


@pytest.mark.parametrize("mb", [10, 15, 17.6, 20])
def test_large_files_assume_a_mobile_data_floor(mb):
    """The implied sustained throughput must be reachable on a phone."""
    budget = fd.download_timeout_seconds(int(mb * 1024 * 1024))
    implied_kbs = mb * 1024 / budget

    assert implied_kbs <= 100, (
        f"{mb}MB/{budget}s demands {implied_kbs:.0f} KB/s sustained")


def test_the_floor_still_applies_to_small_files():
    assert fd.download_timeout_seconds(0) == fd._DOWNLOAD_FLOOR_SECONDS
    assert fd.download_timeout_seconds(1024) == fd._DOWNLOAD_FLOOR_SECONDS


def test_the_budget_stays_bounded_at_the_telegram_cap():
    """Telegram's getFile caps bot downloads at 20 MB, so that is the real
    worst case — the budget must not grow without limit."""
    assert fd.download_timeout_seconds(20 * 1024 * 1024) <= 400


# ── the retry ──────────────────────────────────────────────────────────────

class _Bot:
    """Minimal stand-in: get_file always works, download_file is scripted."""

    def __init__(self, script):
        self.script = list(script)
        self.attempts = 0

    async def get_file(self, _file_id):
        return type("F", (), {"file_path": "p"})()

    async def download_file(self, _path, timeout=None):
        self.attempts += 1
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Bytes:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


async def test_a_fast_transient_failure_is_retried(monkeypatch):
    """The case that costs a teacher a 17 MB re-upload today."""
    bot = _Bot([TimeoutError(), _Bytes(b"ok")])
    monkeypatch.setattr(fd.asyncio, "sleep", _no_sleep)

    out = await fd.fetch_telegram_file(bot, "fid", size_bytes=1024)

    assert out == b"ok"
    assert bot.attempts == 2


async def test_a_successful_first_attempt_is_not_retried():
    bot = _Bot([_Bytes(b"ok")])

    out = await fd.fetch_telegram_file(bot, "fid", size_bytes=1024)

    assert out == b"ok"
    assert bot.attempts == 1


async def test_a_full_budget_timeout_is_NOT_retried(monkeypatch):
    """The whole point of making the retry conditional: if the link could not
    carry the file in its full budget, a second full budget is just another
    five minutes of the teacher's time for the same answer."""
    slow = {"t": 0.0}
    monkeypatch.setattr(fd.time, "monotonic", lambda: slow["t"])

    async def _download(_path, timeout=None):
        slow["t"] += timeout          # consumed the ENTIRE budget
        raise TimeoutError()

    bot = _Bot([TimeoutError()])
    bot.download_file = _download

    out = await fd.fetch_telegram_file(bot, "fid", size_bytes=10 * 1024 * 1024)

    assert out is None
    assert bot.attempts == 0, "a full-budget timeout must not be retried"


async def test_both_attempts_failing_still_returns_none(monkeypatch):
    """The contract every caller relies on: never raise, just return None so
    the handler can show "please resend" and stay in state."""
    bot = _Bot([TimeoutError(), TimeoutError()])
    monkeypatch.setattr(fd.asyncio, "sleep", _no_sleep)

    out = await fd.fetch_telegram_file(bot, "fid", size_bytes=1024)

    assert out is None
    assert bot.attempts == 2


async def test_a_too_big_file_is_never_retried(monkeypatch):
    """Retrying a file Telegram refuses to serve is guaranteed waste."""
    from aiogram.exceptions import TelegramBadRequest

    class _TooBig:
        attempts = 0

        async def get_file(self, _fid):
            _TooBig.attempts += 1
            raise TelegramBadRequest(
                method=None, message="Bad Request: file is too big"
            )

    out = await fd.fetch_telegram_file(_TooBig(), "fid", size_bytes=30 * 1024 * 1024)

    assert out is None
    assert _TooBig.attempts == 1


async def test_an_unrelated_bad_request_still_raises():
    """A DIFFERENT malformed request is a real bug and must surface."""
    from aiogram.exceptions import TelegramBadRequest

    class _Bad:
        async def get_file(self, _fid):
            raise TelegramBadRequest(method=None, message="Bad Request: chat not found")

    with pytest.raises(TelegramBadRequest):
        await fd.fetch_telegram_file(_Bad(), "fid", size_bytes=1024)


async def test_the_retry_is_logged(monkeypatch):
    """A silent retry hides a degrading network until it fails outright."""
    events = []
    monkeypatch.setattr(fd.logger, "warning",
                        lambda e, **kw: events.append(e))
    monkeypatch.setattr(fd.asyncio, "sleep", _no_sleep)
    bot = _Bot([TimeoutError(), _Bytes(b"ok")])

    await fd.fetch_telegram_file(bot, "fid", size_bytes=1024)

    assert any("retry" in e for e in events), f"retry not logged: {events}"
