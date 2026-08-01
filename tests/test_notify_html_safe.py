"""
Regression: the first-charge admin notification contains the literal
"/message <id> <matn>" hint. The Bot's default parse_mode is HTML, so a send
that doesn't opt out parses "<matn>" as a tag and Telegram rejects the whole
message ("can't parse entities: Unsupported start tag"). send_text now forces
parse_mode=None, so the message goes out as plain text.
"""
from types import SimpleNamespace

import pytest

from aiogram.exceptions import TelegramBadRequest


class _HtmlDefaultBot:
    """Mirrors the real Bot: send_message defaults to parse_mode='HTML'; an
    HTML send containing an unsupported tag raises exactly like Telegram."""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, parse_mode="HTML", **k):
        if parse_mode == "HTML" and "<" in (text or "") and ">" in (text or ""):
            raise TelegramBadRequest(
                method=None,
                message='can\'t parse entities: Unsupported start tag "matn" at byte offset 124',
            )
        self.sent.append((chat_id, text, parse_mode))
        return True


async def test_send_text_forces_plain_and_delivers_angle_brackets():
    from app.services.notify import send_text

    bot = _HtmlDefaultBot()
    ok = await send_text(bot, 111, "Yozish: /message 123 <matn>")   # the exact hint shape
    assert ok is True                                    # would have raised under HTML
    assert bot.sent[0][2] is None                        # sent as plain text


async def test_notify_first_charge_delivers_despite_hint(monkeypatch):
    from app.services import admin_notify
    from app.config import settings

    monkeypatch.setattr(settings, "ADMIN_IDS", [777])
    bot = _HtmlDefaultBot()
    user = SimpleNamespace(username="AKA_BlaZzy", full_name="Blazzy", telegram_id=1262284359)

    delivered = await admin_notify.notify_first_charge(bot, user)

    assert delivered == 1                                # the real failing path now succeeds
    body = bot.sent[0][1]
    assert "<matn>" in body                              # the exact content that broke it
    assert bot.sent[0][2] is None                        # plain, no HTML parsing


def test_regression_send_text_never_uses_html():
    # Guard: send_text must pass parse_mode=None so literal angle brackets in any
    # future proactive notification can never reach an HTML parser.
    import inspect
    from app.services import notify
    src = inspect.getsource(notify.send_text)
    assert "parse_mode=None" in src
