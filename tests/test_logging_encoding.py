"""
Logging must never become the failure it was reporting.

THE BUG, reproduced rather than theorised. Stopping the real Postgres
container and hitting the admin panel's /healthz returned 500 instead of the
intended 503 — not because the DB check failed, but because the
`logger.warning(..., exc_info=True)` call in the except block itself raised:

    File "...\\Lib\\encodings\\cp1251.py", line 19, in encode
    UnicodeEncodeError: 'charmap' codec can't encode characters in position 73-74

On Windows sys.stdout inherits the console codepage (cp1251 here, not UTF-8)
with the default errors="strict", so writing a character outside it raises
from inside the write.

TWO THINGS MADE IT REACHABLE, and both are pinned below:

  1. The admin panel never called setup_logging(). With structlog
     unconfigured, get_logger() returns a default PrintLogger that writes with
     a bare print() — and print() propagates an encoding error, where a
     logging.Handler would have swallowed it via handleError(). It also meant
     the panel's logs were not JSON, ignored LOG_LEVEL, and never reached
     logs/testova.log — the very file the panel's own /alerts page reads.

  2. sys.stdout itself was left strict. Measured, the bot was NOT actually
     crashing: with DEBUG=false its JSONRenderer escapes non-ASCII
     (ensure_ascii=True) so nothing unencodable ever reached the stream, and
     with DEBUG=true the StreamHandler swallowed the error — silently DROPPING
     the log line instead. So the stream hardening is what turns a lost line
     into a written one, and the setup_logging() call is what turns a crash
     into a log line at all.

These tests deliberately do not write to the real logs/testova.log: LOG_DIR
and LOG_FILE are redirected to tmp_path, since the panel's /alerts page reads
the real file and test noise there is indistinguishable from real events.
"""
from __future__ import annotations

import io
import logging
import sys

import pytest
import structlog

from app.utils import logging as applog

# U+2192 RIGHTWARDS ARROW — not representable in cp1251.
UNENCODABLE = "→"


def _strict_cp1251_stream() -> io.TextIOWrapper:
    """A stdout that behaves like this machine's console: cp1251, errors
    strict. Writing UNENCODABLE to one of these raises."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1251", newline="")


@pytest.fixture
def isolated_logging(monkeypatch, tmp_path):
    """Keep setup_logging()'s global side effects out of the rest of the suite:
    restore structlog's config and the root handlers, and send the file handler
    to tmp_path instead of the real log the /alerts page reads."""
    monkeypatch.setattr(applog, "LOG_DIR", tmp_path)
    monkeypatch.setattr(applog, "LOG_FILE", tmp_path / "testova.log")
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        yield
    finally:
        root.handlers, root.level = saved_handlers, saved_level
        structlog.reset_defaults()


def test_a_strict_stream_really_does_raise():
    """Guard against the other tests passing vacuously: if this ever stops
    raising, the fixture no longer reproduces the condition being fixed."""
    stream = _strict_cp1251_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write(UNENCODABLE)
        stream.flush()


def test_hardening_makes_an_unencodable_write_survive(monkeypatch):
    """The core of the fix: after hardening, the character degrades to its
    \\uXXXX escape instead of raising."""
    stream = _strict_cp1251_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    applog._harden_stdout_encoding()
    print(f"arrow {UNENCODABLE} here")
    sys.stdout.flush()

    written = stream.buffer.getvalue()
    assert (bytes([0x5c]) + b"u2192") in written, (
        "expected a backslash escape, got: %r" % written
    )


def test_hardening_preserves_the_console_encoding(monkeypatch):
    """errors is changed; encoding is NOT. Forcing UTF-8 onto a cp1251 console
    would render every Uzbek character in this project as mojibake."""
    stream = _strict_cp1251_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    applog._harden_stdout_encoding()

    assert sys.stdout.encoding.lower().replace("-", "") == "cp1251"
    assert sys.stdout.errors == "backslashreplace"


@pytest.mark.parametrize(
    "stream",
    [io.StringIO(), object()],
    ids=["StringIO", "object-without-reconfigure"],
)
def test_hardening_is_best_effort_on_streams_it_cannot_touch(monkeypatch, stream):
    """sys.stdout is not always a reconfigurable TextIOWrapper — pytest's own
    capture object, a StringIO, a closed stream. A logging-setup problem must
    never stop the process from starting (the same contract _file_handler
    already documents)."""
    monkeypatch.setattr(sys, "stdout", stream)
    applog._harden_stdout_encoding()  # must not raise


def test_setup_logging_hardens_stdout(monkeypatch, isolated_logging):
    """Hardening has to happen as part of normal setup, not only when called
    directly — and BEFORE the handler wraps the stream."""
    stream = _strict_cp1251_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    applog.setup_logging()

    assert sys.stdout.errors == "backslashreplace"


def test_logging_an_exception_with_an_unencodable_char_does_not_raise(
    monkeypatch, isolated_logging
):
    """THE regression test for the reported bug: this is the call that turned
    a clean 503 into a 500."""
    stream = _strict_cp1251_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    applog.setup_logging()
    logger = applog.get_logger("test.encoding")

    try:
        raise ConnectionError(f"could not connect {UNENCODABLE} refused")
    except ConnectionError:
        logger.warning("db_unreachable", exc_info=True)  # must not raise

    assert stream.buffer.getvalue(), "the log line was dropped entirely"


def test_the_line_is_written_not_silently_dropped(monkeypatch, isolated_logging):
    """Not raising is not enough. Before the fix, DEBUG=true took the other
    failure mode: StreamHandler.handleError() swallowed the encoding error and
    the line vanished. Observability loss is the quieter half of this bug."""
    monkeypatch.setattr(applog.settings, "DEBUG", True)  # ConsoleRenderer: no escaping
    stream = _strict_cp1251_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    applog.setup_logging()
    applog.get_logger("test.encoding").warning(f"marker {UNENCODABLE} tail")
    for handler in logging.getLogger().handlers:
        handler.flush()

    written = stream.buffer.getvalue()
    assert b"marker" in written and b"tail" in written, (
        "the record was dropped rather than degraded: %r" % written
    )


def test_the_admin_panel_configures_logging_at_import(isolated_logging):
    """Root cause of the reported crash: the panel never called
    setup_logging(), so structlog stayed on its default PrintLogger (a bare
    print(), which propagates encoding errors) and the panel's own logs never
    reached logs/testova.log at all.

    Asserted via structlog.is_configured() rather than by scanning the source,
    so it tests the effect and not the spelling.
    """
    structlog.reset_defaults()
    assert not structlog.is_configured(), "precondition: structlog starts unconfigured"

    import importlib

    import app.admin.web.app as panel
    importlib.reload(panel)

    assert structlog.is_configured(), (
        "importing the admin panel left structlog unconfigured — its logs go to "
        "a PrintLogger, not to logs/testova.log"
    )
