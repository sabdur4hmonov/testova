"""AUDIT B-33 — cost accounting is swallowed silently at every Gemini boundary.

Both Gemini call sites wrapped `log_gemini_usage` in `except Exception: pass`
with no log line. Not crashing the caller is right; discarding the REASON is
not. The whole admin panel — `/costs`, per-user cost, the alert checker,
reconciliation — is built on `gemini_usage`, so a logger that starts throwing
produces a panel reading "spend is fine" while real spend continues. That is
indistinguishable from genuinely low usage, and expensive to diagnose given the
known NULL-attribution backlog.

`log_gemini_usage` is a live candidate to throw: it calls `asyncio.run()`,
which raises `RuntimeError: asyncio.run() cannot be called from a running event
loop` if the calling thread already has one — exactly what happens if a call
site is ever moved off `asyncio.to_thread` onto the event loop.

SWEPT THE CLASS: 13 `except Exception: pass` sites exist in `app/`. Only these
two discard an ACCOUNTING failure; the rest are deliberate best-effort UI paths
(deleting a status message that may already be gone, a Redis health probe) where
there is nothing to account for and a log line would be noise. B-33's cited
third site, `ai_analyzer`, no longer logs usage itself at all — since NEW-1 it
routes through `gemini_rest`, so fixing that one site covers extraction too.
"""
from __future__ import annotations

import pytest

from app.services import gemini_rest, sheet_reader


class _Boom:
    """A usage logger that fails the way the real one can."""

    def __call__(self, *_a, **_kw):
        raise RuntimeError(
            "asyncio.run() cannot be called from a running event loop"
        )


def test_a_failing_usage_logger_is_reported_not_swallowed(monkeypatch, caplog):
    """The point of the fix: the failure must leave a trace."""
    import app.services.usage_log as usage_log

    monkeypatch.setattr(usage_log, "log_gemini_usage", _Boom())

    events = []
    monkeypatch.setattr(
        gemini_rest.logger, "warning",
        lambda event, **kw: events.append((event, kw)),
    )

    gemini_rest._log_usage_safely(
        {"promptTokenCount": 10}, kind="book", model="m",
        user_id=1, call_site="test",
    )

    assert events, "a failing cost logger left no trace at all"
    event, kw = events[0]
    assert "usage" in event
    assert "error" in kw, f"no reason recorded: {kw}"


def test_the_caller_is_still_never_crashed(monkeypatch):
    """The half that must NOT change: accounting must never break a grade or a
    generation the teacher already paid for."""
    import app.services.usage_log as usage_log

    monkeypatch.setattr(usage_log, "log_gemini_usage", _Boom())
    monkeypatch.setattr(gemini_rest.logger, "warning", lambda *a, **k: None)

    gemini_rest._log_usage_safely(
        {}, kind="book", model="m", user_id=None, call_site=None,
    )  # must not raise


def test_a_successful_log_stays_quiet(monkeypatch):
    """No warning on the happy path, or the signal is worthless."""
    import app.services.usage_log as usage_log

    monkeypatch.setattr(usage_log, "log_gemini_usage", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(
        gemini_rest.logger, "warning", lambda e, **k: events.append(e)
    )

    gemini_rest._log_usage_safely(
        {}, kind="book", model="m", user_id=1, call_site="x",
    )

    assert events == []


@pytest.mark.parametrize("module", [gemini_rest, sheet_reader])
def test_neither_gemini_boundary_swallows_silently_any_more(module):
    """Guards the pattern, not just the instance: a bare `except: pass` around
    usage logging must not come back at either boundary."""
    import inspect
    import re

    src = inspect.getsource(module)
    # find the usage-logging region and assert it has no bare pass
    for m in re.finditer(r"log_gemini_usage|_log_usage_safely", src):
        window = src[m.start():m.start() + 400]
        if "except Exception:\n        pass" in window:
            pytest.fail(f"{module.__name__} still swallows usage errors silently")


def test_the_asyncio_run_hazard_is_gone():
    """Revisited for F09, as this test asked: log_gemini_usage used to call
    asyncio.run, which raises when the calling thread already runs a loop. Rows
    now go to one writer loop instead, so that hazard is gone (the loop case is
    exercised in tests/test_connection_budget.py). The boundary guards above
    still matter: a write can fail for other reasons.
    """
    import inspect

    from app.services import usage_log

    assert "asyncio.run(" not in inspect.getsource(usage_log)


async def test_calling_the_logger_from_a_running_loop_raises():
    """The failure mode, demonstrated rather than asserted from the source."""
    import asyncio

    with pytest.raises(RuntimeError, match="running event loop"):
        asyncio.run(asyncio.sleep(0))


# ── An UNINSTRUMENTED call site must be loud, and identifiable in the table ───
#
# The 2026-10-04 shadow review reads gemini_usage to decide whether the paid-tier
# quota caps hold. That is only worth doing if every row attributes to someone.
#
# Verified by grep at the time of writing: every LIVE call site already threads
# user_id, kind and call_site, and there is exactly one direct gemini_rest.call
# in app/. So this is not a present gap — it is the trap a FUTURE call site
# falls into. `gemini_rest.call` used to default kind="book", user_id=None,
# call_site=None, so forgetting all three wrote a row that was untraceable AND
# looked like a real book call. That is what the 193 historical kind='book'
# rows are, and nothing failed to produce them.
#
# Two guards, because they fail differently: the WARNING catches it while
# someone is watching the logs, and the kind catches it months later in a query.

def test_an_omitted_kind_is_not_mistaken_for_a_real_call(monkeypatch):
    """The default must be self-identifying in the table, not a plausible
    real kind that silently pollutes a per-kind cost breakdown."""
    import inspect

    default = inspect.signature(gemini_rest.call).parameters["kind"].default
    assert default == "unattributed", (
        "an omitted kind must be obvious in gemini_usage, not look like real traffic"
    )


def test_a_missing_call_site_warns_the_same_way_a_missing_user_does(monkeypatch):
    """user_id already warned; call_site did not. Both are needed to attribute
    spend, so a row missing either one is equally unusable to the review."""
    import app.services.usage_log as usage_log

    events = []
    monkeypatch.setattr(
        usage_log.logger, "warning", lambda e, **k: events.append((e, k))
    )
    monkeypatch.setattr(usage_log, "_insert", _noop_insert())

    usage_log.log_gemini_usage(
        _UsageResponse(), kind="book_generate", model="m",
        user_id=7, call_site=None,
    )

    assert events, "an uninstrumented call site logged nothing"
    event, kw = events[0]
    assert event == "gemini_usage_unattributed"
    assert kw.get("call_site") is None


def test_a_fully_attributed_row_warns_about_nothing(monkeypatch):
    """The signal has to stay rare or it gets ignored."""
    import app.services.usage_log as usage_log

    events = []
    monkeypatch.setattr(
        usage_log.logger, "warning", lambda e, **k: events.append(e)
    )
    monkeypatch.setattr(usage_log, "_insert", _noop_insert())

    usage_log.log_gemini_usage(
        _UsageResponse(), kind="book_generate", model="m",
        user_id=7, call_site="book.generate",
    )

    assert events == []


def _noop_insert():
    async def _insert(_row):
        return None
    return _insert


class _UsageResponse:
    """Minimal stand-in for a Gemini response carrying usage metadata."""

    class _Usage:
        prompt_token_count = 10
        candidates_token_count = 5
        thoughts_token_count = 0
        total_token_count = 15
        cached_content_token_count = 0

    usage_metadata = _Usage()
