"""AUDIT B-10 — the admin login lockout is bypassable behind the deployment the
docs prescribe.

`rate_limit.py`'s docstring asserted `request.client.host` is "the direct TCP
peer, not X-Forwarded-For". That is FALSE under uvicorn's defaults, verified
against the installed version:

    uvicorn.Config(proxy_headers=...)      default True
    uvicorn.Config(forwarded_allow_ips=…)  default None -> "127.0.0.1"
    ProxyHeadersMiddleware(trusted_hosts=…) default "127.0.0.1"

`admin_main.py` overrode neither. With the documented "HTTPS + reverse proxy"
on the same host the TCP peer IS 127.0.0.1, so it is trusted, so
`scope["client"]` is replaced by the client-supplied `X-Forwarded-For` — and
rotating that header gives unlimited password guesses against a single admin
password.

SWEEPING THE REST OF THE ADMIN AUTH PATH found it otherwise sound — CSRF on
every mutating route, `require_login` on every protected one, `https_only`
session cookie, `_safe_next` closing the open redirect, and `check_credentials`
failing closed with no password hash — with two exceptions, both fixed here:

  * the lockout is the ONLY decision keyed on client IP, so the blast radius is
    contained, but nothing bounded the TOTAL failure rate across IPs;
  * `check_credentials` short-circuited on the username (`user_ok and
    verify_password(...)`), so a wrong username skipped bcrypt entirely and
    returned measurably faster — a username oracle.

The fix is defence in depth, because either half alone leaves a hole: trusting
the right peer makes `client.host` meaningful again, and a global cap bounds
the damage if the panel is nevertheless deployed behind an untrusted proxy.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.admin.web import rate_limit


class _Req:
    """Enough of a Request for the limiter: a client host."""

    def __init__(self, host: str | None):
        self.client = SimpleNamespace(host=host) if host else None


@pytest.fixture(autouse=True)
def _clean():
    rate_limit.reset()
    yield
    rate_limit.reset()


def _fail(host: str, times: int) -> None:
    for _ in range(times):
        rate_limit.record_failure(_Req(host))


# ── the per-IP lockout still works ──────────────────────────────────────────

def test_one_ip_is_locked_out_after_the_attempt_limit():
    _fail("1.2.3.4", rate_limit.MAX_ATTEMPTS)

    locked, remaining = rate_limit.is_locked_out(_Req("1.2.3.4"))

    assert locked is True
    assert remaining > 0


def test_a_successful_login_clears_the_streak():
    _fail("1.2.3.4", rate_limit.MAX_ATTEMPTS - 1)

    rate_limit.record_success(_Req("1.2.3.4"))

    assert rate_limit.is_locked_out(_Req("1.2.3.4"))[0] is False


def test_an_untouched_ip_is_not_locked_out():
    _fail("1.2.3.4", rate_limit.MAX_ATTEMPTS)

    assert rate_limit.is_locked_out(_Req("9.9.9.9"))[0] is False


# ── the bypass ──────────────────────────────────────────────────────────────

def test_rotating_the_client_ip_no_longer_grants_unlimited_guesses():
    """THE bug. A spoofed X-Forwarded-For lands in request.client.host under the
    documented deployment, so every guess arrived from a "fresh" IP and the
    per-IP counter never reached its limit. A global cap makes the attack cost
    the same as attacking from one address."""
    for i in range(rate_limit.MAX_GLOBAL_ATTEMPTS):
        ip = f"10.0.0.{i % 256}"
        assert rate_limit.is_locked_out(_Req(ip))[0] is False, (
            f"locked out too early, after {i} attempts")
        rate_limit.record_failure(_Req(ip))

    # A brand-new address gets no free pass once the global budget is spent.
    locked, remaining = rate_limit.is_locked_out(_Req("172.16.99.1"))
    assert locked is True
    assert remaining > 0


def test_the_global_cap_is_looser_than_the_per_ip_cap():
    """A global lockout is a self-DoS surface, so it must not be reachable by
    one ordinary user fat-fingering their password."""
    assert rate_limit.MAX_GLOBAL_ATTEMPTS > rate_limit.MAX_ATTEMPTS


def test_a_successful_login_clears_the_global_streak_too():
    """The real admin getting in proves the failures were not them, and must
    not leave the panel locked against itself."""
    for i in range(rate_limit.MAX_GLOBAL_ATTEMPTS):
        rate_limit.record_failure(_Req(f"10.0.0.{i % 256}"))

    rate_limit.record_success(_Req("192.168.1.5"))

    assert rate_limit.is_locked_out(_Req("172.16.99.1"))[0] is False


def test_the_global_lockout_expires():
    for i in range(rate_limit.MAX_GLOBAL_ATTEMPTS):
        rate_limit.record_failure(_Req(f"10.0.0.{i % 256}"))
    assert rate_limit.is_locked_out(_Req("172.16.99.1"))[0] is True

    # Wind the streak's start back past the window instead of sleeping.
    rate_limit._global_first_failure -= rate_limit.LOCKOUT_SECONDS + 1

    assert rate_limit.is_locked_out(_Req("172.16.99.1"))[0] is False


def test_a_missing_client_still_gets_counted():
    """`request.client` can be None (ASGI without a peer). It must not become a
    free unlimited bucket."""
    for _ in range(rate_limit.MAX_ATTEMPTS):
        rate_limit.record_failure(_Req(None))

    assert rate_limit.is_locked_out(_Req(None))[0] is True


# ── the root cause: which peer uvicorn trusts ───────────────────────────────

def test_uvicorn_trusts_forwarded_headers_by_default():
    """Pinned because the module docstring asserted the opposite, and the fix's
    whole reason is this default."""
    import inspect

    import uvicorn
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    assert inspect.signature(uvicorn.Config.__init__).parameters[
        "proxy_headers"].default is True
    assert inspect.signature(ProxyHeadersMiddleware.__init__).parameters[
        "trusted_hosts"].default == "127.0.0.1"


def test_the_entrypoint_declares_which_proxies_it_trusts():
    """admin_main.py overrode neither default, so a same-host reverse proxy —
    the documented deployment — made X-Forwarded-For authoritative."""
    import inspect

    import admin_main

    src = inspect.getsource(admin_main)
    assert "forwarded_allow_ips" in src
    assert "ADMIN_TRUSTED_PROXIES" in src


def test_trusting_nothing_is_the_default():
    """An operator who has not declared a proxy must get the real TCP peer,
    which is the behaviour the limiter was written to assume."""
    from app.config import Settings

    s = Settings(TELEGRAM_BOT_TOKEN="1:a")
    assert s.ADMIN_TRUSTED_PROXIES == ""


# ── the username oracle ─────────────────────────────────────────────────────

def test_a_wrong_username_still_costs_a_password_check():
    """check_credentials was `user_ok and verify_password(...)`, so a wrong
    username skipped bcrypt and returned far faster — an oracle for the
    username. Both paths must now do the same work."""
    from app.admin.web import security as sec
    from app.config import settings as app_settings

    real_hash = sec.hash_password("correct-horse")
    old_hash, old_user = app_settings.ADMIN_PASSWORD_HASH, app_settings.ADMIN_PANEL_USERNAME
    app_settings.ADMIN_PASSWORD_HASH = real_hash
    app_settings.ADMIN_PANEL_USERNAME = "admin"
    try:
        def _timed(user: str) -> float:
            t0 = time.perf_counter()
            assert sec.check_credentials(user, "wrong-password") is False
            return time.perf_counter() - t0

        wrong_user = min(_timed("nope") for _ in range(3))
        right_user = min(_timed("admin") for _ in range(3))
    finally:
        app_settings.ADMIN_PASSWORD_HASH = old_hash
        app_settings.ADMIN_PANEL_USERNAME = old_user

    # bcrypt dominates; a skipped hash is orders of magnitude faster. Allowing
    # 3x keeps this from flaking on a noisy runner while still catching a
    # short-circuit, which shows up as ~100x.
    assert wrong_user > right_user / 3, (
        f"wrong username returned in {wrong_user:.6f}s vs {right_user:.6f}s "
        f"for a valid one — the username is still leaking through timing")


def test_credentials_still_fail_closed_without_a_hash():
    from app.admin.web import security as sec
    from app.config import settings as app_settings

    old = app_settings.ADMIN_PASSWORD_HASH
    app_settings.ADMIN_PASSWORD_HASH = ""
    try:
        assert sec.check_credentials("admin", "anything") is False
        assert sec.login_enabled() is False
    finally:
        app_settings.ADMIN_PASSWORD_HASH = old


def test_correct_credentials_still_succeed():
    from app.admin.web import security as sec
    from app.config import settings as app_settings

    old_hash, old_user = app_settings.ADMIN_PASSWORD_HASH, app_settings.ADMIN_PANEL_USERNAME
    app_settings.ADMIN_PASSWORD_HASH = sec.hash_password("correct-horse")
    app_settings.ADMIN_PANEL_USERNAME = "admin"
    try:
        assert sec.check_credentials("admin", "correct-horse") is True
        assert sec.check_credentials("admin", "wrong") is False
        assert sec.check_credentials("nope", "correct-horse") is False
    finally:
        app_settings.ADMIN_PASSWORD_HASH = old_hash
        app_settings.ADMIN_PANEL_USERNAME = old_user
