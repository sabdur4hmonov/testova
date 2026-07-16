"""
Pure time-parsing helpers for the exam timer. No I/O, no DB, no scheduler.

A teacher types a wall-clock time ("14:30", "2:30 PM", "14:30:00", "9:05 am")
and, for the start+duration mode, a plain number of minutes. Everything here is
a pure function so it can be unit-tested exhaustively.

Wall-clock input is anchored to TODAY in a FIXED offset (Uzbekistan = UTC+5, no
DST) and returned as a timezone-aware datetime in UTC, ready for storage and
scheduling.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

# "14:30", "14:30:00", "2:30 PM", "9:05 am", "7 pm", "07:5" (tolerant on digits).
_CLOCK_RE = re.compile(
    r"^\s*(\d{1,2})(?:[:.\s](\d{1,2}))?(?:[:.\s](\d{1,2}))?\s*([ap]\.?m\.?)?\s*$",
    re.IGNORECASE,
)


def parse_clock(text: str | None) -> time | None:
    """
    Parse a wall-clock time. Returns a `time`, or None if unparseable.

    Accepts 24-hour ("14:30", "14:30:00") and 12-hour with am/pm
    ("2:30 PM", "9:05 am", "7 pm"). Minutes/seconds default to 0. Rejects
    out-of-range values (hour > 23, minute/second > 59, and 12-hour hour > 12).
    """
    if not text or not text.strip():
        return None

    m = _CLOCK_RE.match(text)
    if not m:
        return None

    hh_s, mm_s, ss_s, ampm = m.groups()
    hour = int(hh_s)
    minute = int(mm_s) if mm_s is not None else 0
    second = int(ss_s) if ss_s is not None else 0

    if minute > 59 or second > 59:
        return None

    if ampm:
        # 12-hour clock: hour must be 1..12; fold to 0..23.
        if hour < 1 or hour > 12:
            return None
        pm = ampm.lower().startswith("p")
        if hour == 12:
            hour = 12 if pm else 0
        elif pm:
            hour += 12
    else:
        if hour > 23:
            return None

    return time(hour=hour, minute=minute, second=second)


def parse_duration_minutes(text: str | None) -> int | None:
    """
    Parse a duration in minutes: a plain positive integer. Rejects zero,
    negatives, blanks, and anything non-numeric. Returns the int, or None.
    """
    if not text or not text.strip():
        return None
    s = text.strip()
    if not re.fullmatch(r"\d+", s):
        return None
    minutes = int(s)
    if minutes <= 0:
        return None
    return minutes


def offset_tz(offset_hours: int) -> timezone:
    """The fixed exam timezone (e.g. UTC+5 for Uzbekistan)."""
    return timezone(timedelta(hours=offset_hours))


def combine_today(clock: time, offset_hours: int, now: datetime | None = None) -> datetime:
    """
    Anchor a wall-clock time to TODAY in the fixed offset, returned as a
    UTC-aware datetime (ready for DB storage + scheduling).

    `now` is UTC-aware; defaults to the real current time. Kept as a parameter
    so tests are deterministic.
    """
    tz = offset_tz(offset_hours)
    if now is None:
        now = datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    local_dt = datetime.combine(local_now.date(), clock, tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


def compute_end_time(start: datetime, minutes: int) -> datetime:
    """End = start + duration. Both aware; returned in start's tz (UTC here)."""
    return start + timedelta(minutes=minutes)
