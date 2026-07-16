"""time_parser: clock formats, duration, today-anchoring, end computation."""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from app.utils.time_parser import (
    combine_today,
    compute_end_time,
    offset_tz,
    parse_clock,
    parse_duration_minutes,
)


# ── parse_clock: accepted formats ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [
        ("14:30", time(14, 30)),
        ("2:30 PM", time(14, 30)),
        ("2:30pm", time(14, 30)),
        ("14:30:00", time(14, 30, 0)),
        ("09:05", time(9, 5)),
        ("9:05 am", time(9, 5)),
        ("12:00 AM", time(0, 0)),   # midnight
        ("12:00 PM", time(12, 0)),  # noon
        ("7 pm", time(19, 0)),
        ("  14:30  ", time(14, 30)),
        ("00:00", time(0, 0)),
        ("23:59", time(23, 59)),
    ],
)
def test_parse_clock_valid(text, expected):
    assert parse_clock(text) == expected


# ── parse_clock: garbage rejected ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        None, "", "   ", "abc", "25:00", "14:60", "14:30:99",
        "13 pm", "0 am", "hello 14:30", "14-30-xx", "half past two",
    ],
)
def test_parse_clock_invalid(text):
    assert parse_clock(text) is None


# ── parse_duration_minutes ────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [("90", 90), ("1", 1), ("  120 ", 120)])
def test_parse_duration_valid(text, expected):
    assert parse_duration_minutes(text) == expected


@pytest.mark.parametrize(
    "text", [None, "", "0", "-5", "1.5", "90m", "abc", "  ", "9 0"]
)
def test_parse_duration_invalid(text):
    assert parse_duration_minutes(text) is None


# ── combine_today: anchors to today in UTC+5, returns UTC-aware ───────────────
def test_combine_today_utc5_conversion():
    # 2026-07-16 08:00 UTC == 13:00 in UTC+5. A 14:30 local time on that date
    # is 09:30 UTC.
    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    out = combine_today(time(14, 30), 5, now=now)
    assert out.tzinfo == timezone.utc
    assert out == datetime(2026, 7, 16, 9, 30, tzinfo=timezone.utc)


def test_combine_today_uses_local_date_not_utc_date():
    # 22:00 UTC is already the next day (03:00) in UTC+5; "01:00" local must
    # anchor to the LOCAL date, i.e. 2026-07-17 01:00 +05 == 2026-07-16 20:00 UTC.
    now = datetime(2026, 7, 16, 22, 0, tzinfo=timezone.utc)
    out = combine_today(time(1, 0), 5, now=now)
    assert out == datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc)


def test_offset_tz():
    assert combine_today(time(0, 0), 5, now=datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)) \
        .astimezone(offset_tz(5)).hour == 0


# ── compute_end_time ──────────────────────────────────────────────────────────
def test_compute_end_time():
    start = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
    assert compute_end_time(start, 90) == datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


def test_compute_end_time_crosses_midnight():
    start = datetime(2026, 7, 16, 23, 30, tzinfo=timezone.utc)
    assert compute_end_time(start, 60) == datetime(2026, 7, 17, 0, 30, tzinfo=timezone.utc)
