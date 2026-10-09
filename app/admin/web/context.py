"""Small shared helpers for panel routers."""
from __future__ import annotations

from fastapi import Request

from app.config import settings

# Bounds for every `?days=` window the panel accepts (cost page, CSV exports).
# 1 because a zero- or negative-length window is not a question anyone means to
# ask; 365 because the answer is computed by scanning gemini_usage, and an
# unbounded window invites a full-table scan from a URL an attacker can type.
MIN_DAYS = 1
MAX_DAYS = 365
DEFAULT_DAYS = 30


class BadRequest(Exception):
    """A query parameter outside its allowed range. app.py turns this into a
    400 — the same shape as the _CSRFRejected -> 403 handler, so a bad URL is
    answered rather than served a 500 or a silently wrong window."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def validated_days(days: int = DEFAULT_DAYS) -> int:
    """FastAPI dependency: a `?days=` window clamped to [MIN_DAYS, MAX_DAYS].

    Rejects rather than clamps. `?days=-5` silently answered as 30 days is a
    figure the admin cannot tell apart from a real one, and reconciling spend
    against Google's bill is precisely where that matters.
    """
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise BadRequest(
            f"`days` {MIN_DAYS}–{MAX_DAYS} oralig‘ida bo‘lishi kerak (berildi: {days})."
        )
    return days


def acting_admin_id() -> int:
    """telegram_id written to admin_log for actions taken through the panel.
    The single panel admin maps to the first configured ADMIN_IDS entry (0 if
    none configured — still auditable, just not tied to a Telegram account)."""
    return settings.ADMIN_IDS[0] if settings.ADMIN_IDS else 0


def flash(request: Request, message: str, level: str = "success") -> None:
    """Stash a one-shot message in the session, shown after a redirect (PRG)."""
    request.session["_flash"] = {"message": message, "level": level}


def pop_flash(request: Request) -> dict | None:
    return request.session.pop("_flash", None)
