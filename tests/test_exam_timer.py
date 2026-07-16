"""exam_timer: pure job planning + startup reload query behaviour."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.exam_timer import (
    WARNING_MINUTES,
    is_end_in_future,
    plan_jobs,
    reload_pending,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


# ── plan_jobs ─────────────────────────────────────────────────────────────────
def test_plan_jobs_normal_schedules_warn_and_end():
    end = NOW + timedelta(hours=2)
    jobs = plan_jobs(end, NOW)
    kinds = [j.kind for j in jobs]
    assert kinds == ["warn", "end"]
    warn = next(j for j in jobs if j.kind == "warn")
    assert warn.run_at == end - timedelta(minutes=WARNING_MINUTES)
    assert next(j for j in jobs if j.kind == "end").run_at == end


def test_plan_jobs_past_end_returns_nothing():
    end = NOW - timedelta(minutes=1)
    assert plan_jobs(end, NOW) == []


def test_plan_jobs_end_equal_now_returns_nothing():
    assert plan_jobs(NOW, NOW) == []


def test_plan_jobs_under_10_min_skips_warning():
    # Exam ends in 5 minutes → warning would be in the past → skip it, keep end.
    end = NOW + timedelta(minutes=5)
    jobs = plan_jobs(end, NOW)
    assert [j.kind for j in jobs] == ["end"]
    assert jobs[0].run_at == end


def test_plan_jobs_exactly_10_min_skips_warning():
    # warn_at == now (not strictly future) → skip warning, keep end only.
    end = NOW + timedelta(minutes=WARNING_MINUTES)
    assert [j.kind for j in plan_jobs(end, NOW)] == ["end"]


def test_plan_jobs_just_over_10_min_keeps_warning():
    end = NOW + timedelta(minutes=WARNING_MINUTES, seconds=1)
    assert [j.kind for j in plan_jobs(end, NOW)] == ["warn", "end"]


# ── is_end_in_future ──────────────────────────────────────────────────────────
def test_is_end_in_future():
    assert is_end_in_future(NOW + timedelta(seconds=1), NOW) is True
    assert is_end_in_future(NOW, NOW) is False
    assert is_end_in_future(NOW - timedelta(seconds=1), NOW) is False


# ── reload_pending: picks up a project with a future exam_end_time ────────────
class _FakeRow(tuple):
    """A row that unpacks as (project_id, end_time, telegram_id, language)."""


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal async-context session returning canned rows for the reload query."""

    def __init__(self, rows):
        self._rows = rows
        self.executed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _query):
        self.executed = True
        return _FakeResult(self._rows)


def _session_factory(rows):
    def factory():
        return _FakeSession(rows)
    return factory


@pytest.mark.asyncio
async def test_reload_pending_schedules_future_exam(monkeypatch):
    import app.services.exam_timer as et

    # Pretend the scheduler is up; capture schedule_exam calls instead of really
    # scheduling (keeps the test free of a live event-loop scheduler).
    monkeypatch.setattr(et, "_scheduler", object())
    calls = []
    monkeypatch.setattr(
        et, "schedule_exam",
        lambda bot, pid, chat_id, end, lang, now: calls.append((pid, chat_id, end, lang)) or 2,
    )

    end = NOW + timedelta(hours=1)
    rows = [("11111111-1111-1111-1111-111111111111", end, 5037603460, _Lang("uz"))]
    total = await reload_pending(
        bot=object(), session_factory=_session_factory(rows), now=NOW
    )

    assert total == 2
    assert len(calls) == 1
    pid, chat_id, sched_end, lang = calls[0]
    assert chat_id == 5037603460
    assert sched_end == end
    assert lang == "uz"


@pytest.mark.asyncio
async def test_reload_pending_no_scheduler_is_noop(monkeypatch):
    import app.services.exam_timer as et
    monkeypatch.setattr(et, "_scheduler", None)
    total = await reload_pending(
        bot=object(), session_factory=_session_factory([]), now=NOW
    )
    assert total == 0


class _Lang:
    """Stand-in for the Language enum (has a .value)."""
    def __init__(self, value):
        self.value = value
