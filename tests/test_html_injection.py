"""AUDIT B-11 — user-controlled display names are interpolated unescaped into
HTML-parsed messages.

`_fmt_user`, `_user_line` and `cmd_revoke` f-string `full_name`, `username` and
`note` straight into admin replies, and `full_name` is mirrored from Telegram on
every update. One user whose name contains `<` makes Telegram reject the send
with `400 can't parse entities`, breaking `/users`, `/find`, `/user` and
`/grant` for the admin. It also lets a user forge markup — e.g. a `<code>`
-wrapped fake telegram id — in the admin's own view before a `/revoke`.

THE SWEEP FOUND THE CLASS IS MUCH WIDER THAN "messages with parse_mode=HTML".
`create_bot()` sets `DefaultBotProperties(parse_mode=ParseMode.HTML)`, so EVERY
message the bot sends is HTML-parsed whether or not the call passes
`parse_mode` — grepping for `parse_mode="HTML"` finds only the sites that say
so redundantly, and misses the rest. That is why this file pins the default
itself: the day someone drops that default, these tests should be revisited,
and until then every f-string reaching a send is an HTML sink.

Consequently this fixes two places, not the one cited:
  * `admin.py` — `full_name`, `username`, `note` (the reported sites), and
  * `checking.py::_name_line` — the STUDENT name on every grading report,
    which comes from the answer-sheet OCR or the teacher's typed correction.
    Uncited by the audit, same class, and on the highest-frequency path in the
    product.

`book.py` was checked and is already correct — it escapes at the call site
(`html.escape(theme.title)` before `t(...)`), which is why the translation
templates carrying `{title}` are not vulnerabilities.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.bot.handlers.admin import _fmt_user, _user_line
from app.bot.handlers.checking import _name_line

HOSTILE = '<b>pwned</b> & <code>999</code>'


class _User:
    """A User row with attacker-controlled text in every user-supplied field."""

    def __init__(self, **kw):
        self.telegram_id = 12345
        self.full_name = HOSTILE
        self.username = HOSTILE
        self.note = HOSTILE
        self.uses_left = 3
        # _fmt_user compares this against an aware datetime.
        self.access_until = datetime(2027, 1, 1, tzinfo=timezone.utc)
        self.monthly_variant_limit = 10
        self.monthly_check_limit = 10
        self.book_limit = 2
        self.monthly_book_analysis_limit = 1
        self.monthly_book_generation_limit = 5
        self.variant_count_this_period = 0
        self.check_count_this_period = 0
        self.book_analysis_count_this_period = 0
        self.book_generation_count_this_period = 0
        self.is_blocked = False
        self.total_projects = 0
        self.updated_at = datetime(2026, 1, 1, 12, 0)
        self.is_admin = False
        self.plan = None
        self.__dict__.update(kw)


def _has_raw_markup(rendered: str) -> bool:
    """True if attacker text survived as live markup rather than as text."""
    return "<b>pwned</b>" in rendered or "<code>999</code>" in rendered


# ── the reason the class is wide ────────────────────────────────────────────

def test_every_message_is_html_parsed_by_default():
    """Pinned: the bot sets a global HTML parse mode, so `parse_mode="HTML"` at
    a call site is redundant and its ABSENCE proves nothing about safety."""
    import inspect

    from aiogram.enums import ParseMode

    from app.bot import main as bot_main

    src = inspect.getsource(bot_main.create_bot)
    assert "DefaultBotProperties" in src
    assert "ParseMode.HTML" in src or str(ParseMode.HTML) in src


# ── the reported sites ──────────────────────────────────────────────────────

def test_fmt_user_escapes_a_hostile_full_name():
    out = _fmt_user(_User())

    assert not _has_raw_markup(out)
    assert "&lt;b&gt;pwned&lt;/b&gt;" in out


def test_fmt_user_escapes_the_username_and_note():
    out = _fmt_user(_User(full_name="Ordinary Name"))

    assert not _has_raw_markup(out)


def test_user_line_escapes_a_hostile_name():
    out = _user_line(_User())

    assert not _has_raw_markup(out)


def test_a_forged_telegram_id_cannot_be_planted_in_the_admin_view():
    """The attack that matters more than the crash: the admin reads an id out
    of this line before acting on it (e.g. /revoke)."""
    # 16 chars, so it survives _user_line's 18-char truncation intact — the
    # first version of this test used a 19-char payload and passed only because
    # truncation happened to break it, not because anything was escaped.
    payload = "<code>999</code>"
    assert len(payload) <= 18

    out = _user_line(_User(full_name=payload))

    assert payload not in out
    assert "<code>12345</code>" in out, "the REAL id must still be marked up"


def test_an_ampersand_alone_does_not_break_the_send():
    """The most likely real-world trigger — a name like "Ali & Vali" — is not
    an attack at all, just a 400 that breaks /users for everyone."""
    out = _fmt_user(_User(full_name="Ali & Vali", username="ali", note=None))

    assert "&amp;" in out
    assert "Ali & Vali" not in out


# ── the uncited site: the student name on every grading report ─────────────

def test_the_grading_report_escapes_the_student_name():
    line = _name_line(HOSTILE, 3, "uz")

    assert not _has_raw_markup(line)
    assert "&lt;b&gt;" in line


def test_the_grading_report_escapes_a_name_without_a_variant():
    line = _name_line("<i>x</i>", None, "uz")

    assert "<i>x</i>" not in line


def test_a_blank_student_name_still_yields_no_line():
    assert _name_line(None, 1, "uz") is None
    assert _name_line("", 1, "uz") is None


# ── ordinary values must be untouched ──────────────────────────────────────

@pytest.mark.parametrize("name", [
    "Saidakbar Abdurahmonov",
    "O'ktam Qodirov",
    "Гулнора Каримова",
    "Ali-Vali",
])
def test_ordinary_names_render_unchanged(name):
    """Escaping must not LOSE any of the names actually in the database.

    Compared against html.escape's own output, not the raw string: an Uzbek
    apostrophe ("O’ktam" typed as "O'ktam") becomes &#x27;, which Telegram
    renders back as an apostrophe. That is the same convention book.py has
    always used, so matching it keeps one escaping rule across the codebase
    rather than two.
    """
    import html as _html

    expected = _html.escape(name)
    assert expected in _name_line(name, 1, "uz")
    assert expected in _fmt_user(_User(full_name=name, username="u", note=None))
    # _user_line caps the RAW name at 18 chars before escaping, so compare
    # against the escaped form of that cap.
    assert _html.escape(name[:18]) in _user_line(
        _User(full_name=name, username="u", note=None)
    )


def test_the_layout_of_the_user_card_is_unchanged():
    """A regression guard on the surrounding markup: escaping the VALUES must
    not disturb the <code> wrapper around the id or the field labels."""
    out = _fmt_user(_User(full_name="Ali", username="ali", note="vip"))

    assert "<code>12345</code>" in out
    assert "👤" in out and "🔗 Username: @ali" in out and "📝 Izoh: vip" in out


def test_user_line_still_truncates_the_name():
    """The compact row caps the name; escaping must not defeat that cap or the
    row grows unboundedly with a long name."""
    out = _user_line(_User(full_name="A" * 100, username="u", note=None))

    assert "A" * 19 not in out
