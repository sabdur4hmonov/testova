"""AUDIT B-9 — webhook mode accepts forged Telegram updates when
`WEBHOOK_SECRET` is unset.

`WEBHOOK_SECRET` defaults to None and nothing validated it at startup.
aiogram's `SimpleRequestHandler.verify_secret` is:

    if self.secret_token:
        return secrets.compare_digest(telegram_secret_token, self.secret_token)
    return True          # <-- a falsy secret accepts EVERYTHING

The aiohttp server binds 0.0.0.0:8080, so anyone who reaches `/webhook` could
POST an update with an arbitrary `from.id` — including an `ADMIN_IDS` value,
reaching `/grant`, `/broadcast` and `/revoke`.

SWEEP — is the webhook the only unauthenticated entry point sharing this shape?
Checked, and it is the only one:
  * the bot's aiohttp app registers exactly ONE route (`WEBHOOK_PATH`);
    `setup_application` wires startup/shutdown hooks and adds no routes;
  * the other security-critical settings FAIL CLOSED, which is what makes
    WEBHOOK_SECRET the inconsistency rather than the house style —
    `verify_password` returns False on an empty hash, `check_credentials`
    returns False when no hash is configured, and `ADMIN_SESSION_SECRET`
    falls back to a random per-process key (sessions don't survive a restart,
    but they cannot be forged) and logs about it.

So the fix is a startup guard, not a second code path: refuse to start in
webhook mode without a real secret, the same fail-fast the password path
already gets by returning False.
"""
from __future__ import annotations

import pytest

from app.config import Settings, validate_security


def _settings(**kw) -> Settings:
    base = dict(
        TELEGRAM_BOT_TOKEN="123:abc",
        WEBHOOK_URL=None,
        WEBHOOK_SECRET=None,
    )
    base.update(kw)
    return Settings(**base)


# ── the vulnerability itself, pinned so the guard has a documented reason ───

def test_aiogram_accepts_any_secret_when_none_is_configured():
    """Not our code — aiogram's. Pinned because the whole startup guard exists
    to compensate for it, and if a future aiogram makes this fail closed the
    guard's rationale should be revisited rather than cargo-culted."""
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler

    handler = SimpleRequestHandler.__new__(SimpleRequestHandler)
    handler.secret_token = None

    assert handler.verify_secret("anything-at-all", bot=None) is True
    assert handler.verify_secret("", bot=None) is True


def test_a_configured_secret_is_actually_enforced():
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler

    handler = SimpleRequestHandler.__new__(SimpleRequestHandler)
    handler.secret_token = "s3cret"

    assert handler.verify_secret("s3cret", bot=None) is True
    assert handler.verify_secret("wrong", bot=None) is False


# ── the guard ──────────────────────────────────────────────────────────────

def test_webhook_mode_without_a_secret_refuses_to_start():
    s = _settings(WEBHOOK_URL="https://example.com", WEBHOOK_SECRET=None)

    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        validate_security(s)


@pytest.mark.parametrize("secret", ["", "   ", "\t\n"])
def test_a_blank_secret_counts_as_no_secret(secret):
    """A whitespace-only secret is TRUTHY, so aiogram would happily
    compare_digest against it — enforcing a secret nobody could guess but also
    nobody configured on purpose. Treat it as unset."""
    s = _settings(WEBHOOK_URL="https://example.com", WEBHOOK_SECRET=secret)

    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        validate_security(s)


def test_webhook_mode_with_a_real_secret_starts():
    s = _settings(WEBHOOK_URL="https://example.com", WEBHOOK_SECRET="a-real-secret")

    validate_security(s)  # must not raise


def test_polling_mode_does_not_require_a_secret():
    """The live deployment runs polling, where no HTTP server is exposed at
    all. The guard must not break it."""
    s = _settings(WEBHOOK_URL=None, WEBHOOK_SECRET=None)

    validate_security(s)  # must not raise


def test_the_error_names_the_setting_and_says_what_to_do():
    """A fail-fast that doesn't tell the operator how to fix it just gets
    commented out."""
    s = _settings(WEBHOOK_URL="https://example.com")

    with pytest.raises(RuntimeError) as exc:
        validate_security(s)

    msg = str(exc.value)
    assert "WEBHOOK_SECRET" in msg
    assert "forged" in msg.lower() or "anyone" in msg.lower()


# ── the sweep, asserted rather than described ──────────────────────────────

def test_the_other_security_settings_fail_closed():
    """Why this is an inconsistency and not the house style."""
    from app.admin.web import security as sec

    assert sec.verify_password("anything", "") is False
    assert sec.verify_password("anything", "not-a-bcrypt-hash") is False


def test_the_bot_server_exposes_only_the_webhook_route():
    """If a second route is ever added to the bot's aiohttp app, it needs its
    own authentication decision — this test makes that a deliberate act."""
    import inspect
    import main as bot_main

    src = inspect.getsource(bot_main)
    # One handler registration, on the configured path.
    assert src.count(".register(app, path=") == 1
    assert "SimpleRequestHandler" in src
