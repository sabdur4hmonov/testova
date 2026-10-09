"""
Auth for the HTML admin panel: bcrypt password check + a session-login guard.

Single admin. The password lives ONLY as a bcrypt hash in settings
(ADMIN_PASSWORD_HASH); this module never stores or compares a plaintext
password beyond the moment it verifies one login attempt. Generate a hash with:

    python -m app.admin.web.security 'your-password'

and paste the printed line into .env. An empty hash disables login entirely
(the login page says so) — there is deliberately no default password.

Brute-force protection on the login POST IS implemented, in the sibling
module `rate_limit` (per-IP failure streak + a global cross-IP backstop, both
with an expiring window). `routers/auth.py` calls is_locked_out / record_failure
/ record_success around every attempt, so a locked-out POST never reaches
check_credentials below. This module deliberately holds no throttling of its
own — there is exactly one place that counts failures.
"""
from __future__ import annotations

import secrets

import bcrypt
from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import settings

# Marker key in the signed session cookie. Presence + True = logged in.
_SESSION_KEY = "admin_authed"
# Per-session CSRF token, issued at login (see mark_logged_in / get_csrf_token).
_CSRF_KEY = "csrf_token"


def hash_password(plain: str) -> str:
    """bcrypt hash of a plaintext password, as a str safe to store in .env."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt check. False (never raises) on an empty/garbage hash
    so a missing ADMIN_PASSWORD_HASH simply fails every login instead of 500ing."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def login_enabled() -> bool:
    """True only when an admin password hash is configured."""
    return bool(settings.ADMIN_PASSWORD_HASH)


def check_credentials(username: str, password: str) -> bool:
    """Validate one login attempt against the single configured admin."""
    if not login_enabled():
        return False
    user_ok = secrets.compare_digest(
        username.strip(), settings.ADMIN_PANEL_USERNAME
    )
    # AUDIT B-10 (found while sweeping the auth path). The bcrypt check runs
    # even when the username is wrong: `user_ok and verify_password(...)` would
    # short-circuit, so a wrong username returned ~100x faster than a right one
    # and leaked which username exists.
    password_ok = verify_password(password, settings.ADMIN_PASSWORD_HASH)
    return user_ok and password_ok


def mark_logged_in(request: Request) -> None:
    # Start from an EMPTY session rather than writing the marker into whatever
    # the old cookie held. `logout()` clears only the auth marker and the CSRF
    # token, so without this a flash queued but never displayed would surface
    # in the NEXT session that logs in. Starlette's session is a signed cookie
    # with no server-side id, so this is not fixation defence — it is simply
    # making "a login yields a fresh session" true by construction.
    request.session.clear()
    request.session[_SESSION_KEY] = True
    request.session[_CSRF_KEY] = secrets.token_urlsafe(32)


def logout(request: Request) -> None:
    request.session.pop(_SESSION_KEY, None)
    request.session.pop(_CSRF_KEY, None)


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(_SESSION_KEY))


def get_csrf_token(request: Request) -> str:
    """Get-or-create the per-session CSRF token. Templates call this to render
    the hidden field. The get-or-create fallback (rather than requiring one
    from mark_logged_in only) means a session already logged in before this
    existed still gets a token on its next page render, instead of every
    mutating form being rejected until the admin logs out and back in."""
    tok = request.session.get(_CSRF_KEY)
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session[_CSRF_KEY] = tok
    return tok


def verify_csrf(request: Request, submitted: str | None) -> bool:
    """Constant-time compare of a submitted token against the session's.
    False (never raises) on a missing session token or missing/blank submission."""
    expected = request.session.get(_CSRF_KEY)
    if not expected or not submitted:
        return False
    return secrets.compare_digest(expected, submitted)


def require_login(request: Request) -> RedirectResponse | None:
    """FastAPI dependency: allow through if logged in, else redirect to /login.

    Used as `dependencies=[Depends(require_login)]` on protected routes. Raising
    is avoided in favour of a redirect so an unauthenticated hit lands on the
    login form rather than a bare 401.
    """
    if is_logged_in(request):
        return None
    # A dependency can't itself redirect, so we raise a special exception the
    # app's handler turns into a redirect (see app.py).
    raise _NotAuthenticated()


class _NotAuthenticated(Exception):
    """Internal: signals require_login to redirect to the login page."""


async def require_csrf(request: Request) -> None:
    """FastAPI dependency for every state-changing (POST) route: validate the
    csrf_token form field against the session's token before the handler body
    runs. List AFTER Depends(require_login) on a route so a session already
    exists to check against.

    Reads the body via request.form(), which Starlette caches — the route's
    own Form(...) parameters re-use that cached parse, so the body is only
    read off the wire once.
    """
    form = await request.form()
    token = form.get("csrf_token")
    if not verify_csrf(request, token if isinstance(token, str) else None):
        raise _CSRFRejected()


class _CSRFRejected(Exception):
    """Internal: signals require_csrf to reject the request with 403 (see app.py)."""


if __name__ == "__main__":  # `python -m app.admin.web.security <password>`
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m app.admin.web.security '<password>'")
        raise SystemExit(2)
    print("ADMIN_PASSWORD_HASH=" + hash_password(sys.argv[1]))
