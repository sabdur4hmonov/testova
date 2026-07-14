"""
One source of truth for the support/admin contact handle: settings.ADMIN_USERNAME.
No user-facing string may hardcode an "@testova_" handle.
"""
from __future__ import annotations

import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


def test_no_hardcoded_testova_handle_in_source():
    offenders = []
    for py in APP_DIR.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "@testova_" in text:
            for i, line in enumerate(text.splitlines(), 1):
                if "@testova_" in line:
                    offenders.append(f"{py.relative_to(APP_DIR.parent)}:{i}: {line.strip()}")
    assert not offenders, "hardcoded @testova_ handle(s):\n" + "\n".join(offenders)


def test_support_and_help_use_admin_username(monkeypatch):
    # both handlers must render the ONE configured handle, not a literal
    from app.config import settings
    from app.bot.handlers import settings as settings_handler  # noqa: F401
    from app.bot.handlers import start as start_handler  # noqa: F401

    # the source references settings.ADMIN_USERNAME (single source of truth)
    for mod in ("app/bot/handlers/settings.py",
                "app/bot/handlers/start.py",
                "app/tasks/notification_tasks.py"):
        src = (APP_DIR.parent / mod).read_text(encoding="utf-8")
        assert "settings.ADMIN_USERNAME" in src, mod
    assert settings.ADMIN_USERNAME  # configured
