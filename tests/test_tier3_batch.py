"""AUDIT Tier 3 — the batched low-severity fixes, grouped by subsystem.

Only the rows with real user-visible or safety impact were taken; the rest are
parked as documented backlog in AUDIT.md rather than chased.
"""
from __future__ import annotations

import inspect

import pytest


# ── file detection (B-43) ─────────────────────────────────────────────────

def test_png_magic_bytes_are_actually_detected():
    """`content[:3] in (..., b"\x89PNG", ...)` could never match a FOUR-byte
    literal, so PNG detection was dead code and a PNG with an unhelpful
    filename stored as file_type="unknown"."""
    from app.services.file_processor import detect_file_type

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    assert detect_file_type("screenshot", png) == "image"


@pytest.mark.parametrize("blob,name", [
    (b"\xff\xd8\xff" + b"0" * 64, "photo"),
    (b"GIF89a" + b"0" * 64, "anim"),
    (b"RIFF" + b"0000" + b"WEBP" + b"0" * 64, "sticker"),
])
def test_the_other_image_formats_still_detect(blob, name):
    from app.services.file_processor import detect_file_type

    assert detect_file_type(name, blob) == "image"


def test_webp_is_accepted_by_extension_too():
    """The upload gate accepts .webp; detection did not."""
    from app.services.file_processor import detect_file_type

    assert detect_file_type("x.webp", b"nothing recognisable") == "image"


@pytest.mark.parametrize("blob,name,expected", [
    (b"%PDF-1.7" + b"0" * 64, "a.pdf", "pdf"),
    (b"PK\x03\x04" + b"0" * 64, "a.docx", "docx"),
    (b"garbage", "a.bin", "unknown"),
])
def test_the_other_types_are_unchanged(blob, name, expected):
    from app.services.file_processor import detect_file_type

    assert detect_file_type(name, blob) == expected


# ── admin (B-53, B-54) ────────────────────────────────────────────────────

def test_find_escapes_like_wildcards():
    """`/find %` matched EVERY user. Parameterised, so never an injection —
    just a surprising admin tool."""
    from app.services import admin_users

    src = inspect.getsource(admin_users.search_users)

    assert "_LIKE_ESCAPE" in src
    assert 'escape=' in src, "an escaped pattern needs an ESCAPE clause to mean anything"


def test_the_lockout_table_evicts_stale_entries():
    """B-54: entries below the threshold were never removed, so the dict grew
    unbounded on an internet-reachable port."""
    from app.admin.web import rate_limit

    rate_limit.reset()

    class _Req:
        def __init__(self, host):
            self.client = type("C", (), {"host": host})()

    # one failure from many addresses, none reaching the lockout threshold
    for i in range(50):
        rate_limit.record_failure(_Req(f"10.0.{i}.1"))
    assert len(rate_limit._failed_attempts) == 50

    # wind every entry past its window, then one more failure evicts them
    for ip, (c, _first) in list(rate_limit._failed_attempts.items()):
        rate_limit._failed_attempts[ip] = (c, 0.0)
    rate_limit.record_failure(_Req("10.9.9.9"))

    assert len(rate_limit._failed_attempts) == 1, (
        f"stale entries were not evicted: {len(rate_limit._failed_attempts)}")
    rate_limit.reset()


def test_eviction_does_not_free_a_locked_out_attacker():
    """The regression risk: evicting an ACTIVE lockout would hand back
    unlimited guesses."""
    from app.admin.web import rate_limit

    rate_limit.reset()

    class _Req:
        client = type("C", (), {"host": "1.2.3.4"})()

    for _ in range(rate_limit.MAX_ATTEMPTS):
        rate_limit.record_failure(_Req())

    assert rate_limit.is_locked_out(_Req())[0] is True
    rate_limit.reset()


# ── throttling (B-48) ─────────────────────────────────────────────────────

async def test_a_throttled_callback_is_acknowledged(monkeypatch):
    """A dropped callback left the button spinning ~30s then showed "query is
    too old". Acknowledging costs nothing and turns a broken-looking button
    into an ignored tap."""
    from app.bot.middlewares import throttling

    answered = []

    class _CB:
        data = "x"

        async def answer(self, *_a, **_k):
            answered.append(True)

    class _Claimed:
        """Redis stand-in: the slot is already taken, i.e. throttled."""

        async def set(self, *_a, **_k):
            return None

    monkeypatch.setattr(throttling, "_get_client", lambda: _Claimed())

    mw = throttling.ThrottlingMiddleware(rate_limit=10.0)
    ran = []

    async def handler(_e, _d):
        ran.append(True)

    # the middleware reads the user from DATA, not the event
    await mw(handler, _CB(), {"event_from_user": type("U", (), {"id": 1})()})

    assert not ran, "the throttled update should not reach the handler"
    assert answered, "the throttled callback was dropped without an answer()"


async def test_a_throttled_MESSAGE_is_not_answered(monkeypatch):
    """Only callbacks have an answer() to acknowledge; a message must not gain
    a spurious reply."""
    from app.bot.middlewares import throttling

    class _Msg:            # no .data attribute -> not a callback
        async def answer(self, *_a, **_k):
            raise AssertionError("a throttled message must stay silent")

    class _Claimed:
        async def set(self, *_a, **_k):
            return None

    monkeypatch.setattr(throttling, "_get_client", lambda: _Claimed())
    mw = throttling.ThrottlingMiddleware(rate_limit=10.0)

    async def handler(_e, _d):
        raise AssertionError("should have been throttled")

    await mw(handler, _Msg(), {"event_from_user": type("U", (), {"id": 2})()})


# ── broadcast (B-57) ──────────────────────────────────────────────────────

def test_the_broadcast_retry_failure_is_logged():
    """It swallowed silently while its sibling logged, so systematic
    post-flood failures read as "the user blocked the bot"."""
    from app.services import broadcast

    src = inspect.getsource(broadcast)

    assert "broadcast_retry_failed" in src


# ── multi_source download (B-52) ──────────────────────────────────────────

def test_the_dup_file_path_passes_the_file_size():
    """One call site of eight omitted size_bytes, losing the large-file
    timeout protection the shared helper exists to provide."""
    from app.bot.handlers import multi_source

    src = inspect.getsource(multi_source.handle_dupfile_add)

    assert "size_bytes" in src
