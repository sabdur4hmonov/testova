"""Secrets never appear in a Settings repr, and so never in an error trace.

FOUND 2026-09-21: monkeypatching a setting that did not exist yet raised
pytest's AttributeError, whose message is the model's repr - every field,
including BOT_TOKEN, the database password inside DATABASE_URL, the Redis
password and the admin session secret, printed into the test output.

Two layers:
  * SecretStr for the secrets whose every reader is outside app/admin/:
    GEMINI_API_KEY, WEBHOOK_SECRET, S3_ACCESS_KEY, S3_SECRET_KEY. Readers go
    through config.secret_value(), which also accepts a plain str so tests that
    assign one keep working.
  * a masked repr for EVERY secret field, including the ones app/admin/ reads
    as plain str (BOT_TOKEN, ADMIN_PASSWORD_HASH, ADMIN_SESSION_SECRET) and the
    password part of the DB / Redis / Celery URLs, which many modules use as str.
"""
from __future__ import annotations

import pytest
import requests
from pydantic import SecretStr

from app.config import Settings, secret_value, validate_security

SECRETS = {
    "BOT_TOKEN": "111:bot-ZQLEAK-token",
    "GEMINI_API_KEY": "gem-ZQLEAK-key",
    "WEBHOOK_SECRET": "hook-ZQLEAK",
    "ADMIN_PASSWORD_HASH": "$2b$12$hash-ZQLEAK",
    "ADMIN_SESSION_SECRET": "session-ZQLEAK",
    "S3_ACCESS_KEY": "s3-access-ZQLEAK",
    "S3_SECRET_KEY": "s3-secret-ZQLEAK",
    "DATABASE_URL": "postgresql+asyncpg://testova:dbpass-ZQLEAK@localhost:5432/testova_db",
    "REDIS_URL": "redis://:redispass-ZQLEAK@localhost:6379/0",
    "CELERY_BROKER_URL": "redis://:brokerpass-ZQLEAK@localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://:backendpass-ZQLEAK@localhost:6379/2",
}


@pytest.fixture
def cfg(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_the_repr_carries_no_secret(cfg):
    assert "ZQLEAK" not in repr(cfg)
    assert "ZQLEAK" not in str(cfg)


def test_the_error_path_that_leaked_carries_no_secret(cfg, monkeypatch):
    """The real leak: pytest's monkeypatch.setattr on a setting that does not
    exist raises AttributeError with repr(target) in its message."""
    with pytest.raises(AttributeError) as err:
        monkeypatch.setattr(cfg, "THIS_SETTING_DOES_NOT_EXIST", 1)
    assert "ZQLEAK" not in str(err.value)


def test_urls_keep_their_non_secret_parts_in_the_repr(cfg):
    r = repr(cfg)
    assert "localhost:5432/testova_db" in r and "localhost:6379/0" in r


@pytest.mark.parametrize("name", ["GEMINI_API_KEY", "WEBHOOK_SECRET", "S3_ACCESS_KEY", "S3_SECRET_KEY"])
def test_these_secrets_are_secretstr(cfg, name):
    value = getattr(cfg, name)
    assert isinstance(value, SecretStr)
    assert secret_value(value) == SECRETS[name]


def test_str_settings_that_app_admin_reads_stay_str_and_unchanged(cfg):
    """app/admin/ reads these as plain str and is out of scope; the repr mask
    is their protection."""
    for name in ("BOT_TOKEN", "ADMIN_PASSWORD_HASH", "ADMIN_SESSION_SECRET", "DATABASE_URL", "REDIS_URL"):
        assert getattr(cfg, name) == SECRETS[name]


def test_secret_value_accepts_plain_strings_and_none():
    assert secret_value("plain") == "plain"
    assert secret_value(None) == ""
    assert secret_value(SecretStr("x")) == "x"


def test_the_real_key_still_reaches_gemini(monkeypatch):
    from app.config import settings
    from app.services import gemini_rest, sheet_reader

    monkeypatch.setattr(settings, "GEMINI_API_KEY", SecretStr("gem-real-key"))
    seen = []

    class _Http:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"usageMetadata": {}, "candidates": [{"finishReason": "STOP",
                    "content": {"parts": [{"text": "{}"}]}}]}

    monkeypatch.setattr(requests, "post",
                        lambda url, headers=None, **kw: seen.append(headers["x-goog-api-key"]) or _Http())
    gemini_rest.call_book_analysis("p")
    sheet_reader._call_sync("p", b"png")
    assert seen == ["gem-real-key", "gem-real-key"]


def test_the_webhook_guard_still_refuses_an_empty_secret(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setenv("WEBHOOK_SECRET", "   ")
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        validate_security(Settings())
    monkeypatch.setenv("WEBHOOK_SECRET", "real-secret")
    validate_security(Settings())
