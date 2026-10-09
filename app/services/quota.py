"""
Independent monthly quotas (variant generation + answer checking), separate
from uses_left. Service layer so the Telegram handlers and a future web
dashboard share one enforcement path.

Model: each user has monthly_variant_limit / monthly_check_limit (NULL =
unlimited) and per-period counters, all sharing ONE rolling 30-day window
anchored at period_start. monthly_variant_limit is the SHARED test pool —
book generation and variant generation both consume quota.TEST against it.

Bepul is the exception on BOTH dimensions, because its allowances are lifetime
rather than monthly: generation rides users.uses_left (outside this module
entirely) and checking rides users.check_count_lifetime, which no reset
touches. plans.is_trial is the single predicate deciding that.

The reset is LAZY: when a user acts and 30 days have elapsed since
period_start (or it was never set), the window restarts NOW and every period
counter zeroes — no cron, no calendar month.

Lookup is by telegram_id (an int); an unknown telegram_id is treated as
unlimited (never blocks), which keeps the deep grading/generation handlers
callable in tests with stand-in users that have no DB row.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings

from app.models.user import User
from app.services import plans
from app.services.usage_log import usage_summary
from app.utils.logging import get_logger

logger = get_logger(__name__)

PERIOD = timedelta(days=30)

# AUDIT B-32. Every gate below FAILS OPEN on any exception, and that stays: a
# quota hiccup must never block a teacher mid-exam, and there is no global
# dp.errors handler, so an exception escaping a gate into a live Telegram
# handler would likely hang the flow silently — worse than being occasionally
# too permissive. What changes is that a real BUG (a typo'd kind, which _fields
# raises ValueError for) is no longer indistinguishable from routine DB-hiccup
# noise: only the errors below keep the routine warning, and anything else logs
# at ERROR under its own event name so monitoring can see it.
#
# OSError covers ConnectionError and TimeoutError (both subclasses since 3.10),
# so a socket-level failure classifies as infra without being listed.
_INFRA_ERRORS = (SQLAlchemyError, OSError)


def _log_failopen(warn_event: str, error_event: str, exc: Exception, **fields) -> None:
    """One place deciding hiccup-vs-bug, so the four gates cannot drift apart."""
    if isinstance(exc, _INFRA_ERRORS):
        logger.warning(warn_event, error=str(exc), **fields)
    else:
        logger.error(error_event, error=str(exc),
                     error_type=type(exc).__name__, **fields)

# TEST — the SHARED pool. One test is one test, whether it came from a book or
# from an uploaded file, so BOTH paths consume this single kind: book.py's
# generation gate and upload.py / multi_source.py's. Standart 20 / Pro 40.
#
# It maps to the columns the variant meter always used (monthly_variant_limit /
# variant_count_this_period), which is why merging the two meters needed NO
# migration and why every existing admin setter, counter value and plan
# derivation keeps working untouched.
TEST = "test"
# The old name, kept as an alias so no call site or test silently changes
# meaning. `_fields` also still accepts the literal "variant".
VARIANT = TEST
CHECK = "check"
# Book ANALYSIS only — the recurring paid Gemini pass over a NEW book. The cap
# on how many books a teacher may OWN is users.book_limit, which is a total and
# never resets, so it is deliberately NOT a kind here: it has no counter and no
# period. See app/models/user.py.
#
# RETIRED: its one consumption site is unreachable (AUDIT C-38 — the TOC flow
# it belonged to was replaced by the page-number path). Kept so the kind and
# the column stay consistent; do not revive without first making it reachable.
BOOK_ANALYSIS = "book_analysis"
# RETIRED: book generation now consumes TEST above, from the shared pool. The
# kind and its columns are left in place (dropping them is not additive), but
# nothing consumes them any more.
BOOK_GENERATION = "book_generation"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fields(kind: str) -> tuple[str, str]:
    # "variant" is accepted alongside TEST: the constant was renamed when the
    # two generation meters merged, and a stray literal must not raise.
    if kind in (TEST, "variant"):
        return "monthly_variant_limit", "variant_count_this_period"
    if kind == CHECK:
        return "monthly_check_limit", "check_count_this_period"
    if kind == BOOK_ANALYSIS:
        return "monthly_book_analysis_limit", "book_analysis_count_this_period"
    if kind == BOOK_GENERATION:
        return "monthly_book_generation_limit", "book_generation_count_this_period"
    raise ValueError(f"unknown quota kind: {kind!r}")


def _is_lifetime(user: User, kind: str) -> bool:
    """Does this user meter `kind` for LIFE rather than per rolling window?

    Only Bepul, and only CHECK. The free trial gets exactly 2 answer-sheet
    checks EVER, which the period counters cannot express: they all hang off
    the one period_start and _apply_rolling_reset zeroes them together, so a
    trial capped through check_count_this_period would refill every 30 days.

    Trial GENERATION is absent because it is not metered by this module at
    all — it rides users.uses_left, which is already lifetime (written once by
    apply_trial, only ever decremented, in no reset). See plans.is_trial.
    """
    return kind == CHECK and plans.is_trial(user)


def _count_attr(user: User, kind: str) -> str:
    """The counter column this user's `kind` actually reads and writes. Every
    path (consume, peek, display) MUST go through this, or a teacher is shown
    one number and gated on another."""
    if _is_lifetime(user, kind):
        return "check_count_lifetime"
    return _fields(kind)[1]


def _window_rolled_over(user: User, now: datetime) -> bool:
    return user.period_start is None or (now - user.period_start) >= PERIOD


def _shadow_enabled(shadow: bool | None) -> bool:
    """None (the handler default) means "whatever the operator configured".
    Explicit True/False is for tests and for paths that must never shadow."""
    return settings.QUOTA_SHADOW_MODE if shadow is None else bool(shadow)


def _apply_rolling_reset(user: User, now: datetime) -> bool:
    """If the 30-day window elapsed (or was never started), restart it NOW and
    zero EVERY period counter. Returns True if a reset happened.

    All counters share the one window anchored at period_start, so they must
    all be zeroed together: a counter left out here would never reset and would
    permanently lock its dimension once the limit was reached.

    check_count_lifetime is deliberately NOT here, and that exact property is
    the reason it exists — Bepul's 2 checks are a LIFETIME cap, so "never
    resets" is the requirement rather than the bug. Nothing else may join that
    exemption without a column of its own: do not teach this function to skip
    a shared counter conditionally.
    """
    if user.period_start is None or (now - user.period_start) >= PERIOD:
        user.period_start = now
        user.variant_count_this_period = 0
        user.check_count_this_period = 0
        user.book_analysis_count_this_period = 0
        user.book_generation_count_this_period = 0
        return True
    return False


async def try_consume(
    session, telegram_id: int, kind: str, now: datetime | None = None,
    shadow: bool = False,
) -> tuple[bool, int | None]:
    """Atomically apply the rolling reset, then consume one unit of `kind` if the
    limit allows. Returns (allowed, remaining); remaining is None when unlimited.
    A NULL limit or an unknown user is unlimited (never blocks).

    `shadow` defaults to False so this stays a pure enforcement primitive; the
    handler-facing wrappers below are where the configured mode is read. Under
    shadow the unit is consumed ANYWAY, past the limit, and allowed: the point
    of the cycle is to learn how far over a real teacher goes, which a counter
    pinned at the cap cannot tell you.
    """
    now = now or _now()
    limit_attr = _fields(kind)[0]
    res = await session.execute(
        select(User).where(User.telegram_id == telegram_id).with_for_update()
    )
    user = res.scalar_one_or_none()
    if user is None:
        return True, None  # unknown user → don't block

    limit = getattr(user, limit_attr)
    if limit is None:
        return True, None  # unlimited on this dimension

    count_attr = _count_attr(user, kind)
    # Shadow covers the PAID caps only. Bepul is enforced from day one: it is
    # trial access against zero revenue, so shadowing it would leave the free
    # tier exactly as unmetered as it was before any of this existed.
    shadow = shadow and not plans.is_trial(user)

    _apply_rolling_reset(user, now)
    count = getattr(user, count_attr)
    if count >= limit:
        if shadow:
            setattr(user, count_attr, count + 1)
            await session.commit()
            logger.warning(
                "quota_would_block", telegram_id=telegram_id, kind=kind,
                limit=limit, count=count + 1,
            )
            return True, 0
        await session.commit()  # persist a reset if one happened
        return False, 0

    setattr(user, count_attr, count + 1)
    await session.commit()
    remaining = limit - (count + 1)
    logger.info("quota_consumed", telegram_id=telegram_id, kind=kind, remaining=remaining)
    return True, remaining


def remaining(user, kind: str, now: datetime | None = None) -> int | None:
    """Units of `kind` left this window for a user OBJECT (pure, no DB). None =
    unlimited (NULL limit). An expired window reads as the full limit again —
    except on a lifetime meter, which has no window to expire."""
    now = now or _now()
    limit = getattr(user, _fields(kind)[0])
    if limit is None:
        return None
    # A lifetime meter has no window to expire, so the rollover shortcut must
    # not apply to it — it would show an exhausted trial its full allowance.
    if not _is_lifetime(user, kind) and _window_rolled_over(user, now):
        return limit
    return max(0, limit - getattr(user, _count_attr(user, kind)))


async def has_quota(session, telegram_id: int, kind: str, now: datetime | None = None,
                    shadow: bool = False) -> bool:
    """READ-ONLY: is there at least one unit of `kind` available? Does NOT
    consume and does NOT write (an expired window reads as full, but the actual
    reset is written only when try_consume runs). Unknown user / NULL limit =
    unlimited. Used to block BEFORE a paid Gemini extraction."""
    now = now or _now()
    res = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = res.scalar_one_or_none()
    if user is None:
        return True
    limit = getattr(user, _fields(kind)[0])
    if limit is None:
        return True
    # The peek must agree with the gate: if shadow will let the generation
    # through, blocking here would stop it BEFORE the extraction instead.
    if shadow and not plans.is_trial(user):
        return True
    if not _is_lifetime(user, kind) and _window_rolled_over(user, now):
        return True  # window rolled over → effectively a fresh full allowance
    return getattr(user, _count_attr(user, kind)) < limit


async def check_available(session_factory, telegram_id: int, kind: str,
                          shadow: bool | None = None) -> bool:
    """Handler-facing peek used before extraction: True if a unit is available.
    A genuine 'exhausted' returns False; an infra error FAILS OPEN (True) so a
    hiccup never blocks a teacher. No consumption — the decrement still happens
    later at generation via check_and_consume."""
    try:
        async with session_factory() as session:
            return await has_quota(session, telegram_id, kind,
                                   shadow=_shadow_enabled(shadow))
    except Exception as e:
        _log_failopen("quota_peek_failopen", "quota_peek_unexpected_error",
                      e, kind=kind)
        return True


# ── Book library: the TOTAL ownership cap ────────────────────────────────────
# Deliberately NOT modelled as a quota `kind`. The kinds above are rolling
# monthly counters; this one is a cap on a LIVE COUNT of rows, with no period
# and no counter column — so there is nothing to reset and nothing that can
# drift out of sync with reality (deleting a book frees a slot immediately).


async def books_owned(session, user_id) -> int:
    """How many books this user (by users.id UUID) currently owns."""
    from sqlalchemy import func as sa_func

    from app.models.book import Book

    res = await session.execute(
        select(sa_func.count()).select_from(Book).where(Book.user_id == user_id)
    )
    return int(res.scalar_one() or 0)


async def book_slots_left(session, telegram_id: int) -> int | None:
    """Books the teacher may still add. None = unlimited (NULL limit, unknown
    user, or admin-style no-limit). 0 = at the cap."""
    res = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = res.scalar_one_or_none()
    if user is None or user.book_limit is None:
        return None
    return max(0, user.book_limit - await books_owned(session, user.id))


async def can_add_book(session_factory, telegram_id: int,
                       shadow: bool | None = None) -> bool:
    """Handler-facing gate for adding a book. Mirrors check_and_consume's
    contract: a genuine 'at the cap' returns False, but an infrastructure error
    FAILS OPEN so a DB hiccup never blocks a teacher."""
    try:
        async with session_factory() as session:
            left = await book_slots_left(session, telegram_id)
        if left is None or left > 0:
            return True
        if _shadow_enabled(shadow):
            # No limit/count fields: this cap is a live COUNT(*) of rows, not a
            # counter, so "how far over" is not a number this gate holds. The
            # event says a teacher hit the cap; books_owned gives the rest.
            logger.warning("quota_would_block", telegram_id=telegram_id,
                           kind="book_limit")
            return True
        return False
    except Exception as e:
        _log_failopen("book_cap_failopen", "book_cap_unexpected_error", e)
        return True


async def check_and_consume(session_factory, telegram_id: int, kind: str,
                            shadow: bool | None = None) -> bool:
    """Handler-facing gate: open a session and consume one unit. Returns whether
    the action is ALLOWED. A genuine 'over limit' returns False (blocks); but an
    infrastructure error (DB down, etc.) FAILS OPEN with a warning — a quota
    hiccup must never block a teacher mid-exam. Enforcement correctness itself is
    covered by try_consume's own tests against a real DB."""
    try:
        async with session_factory() as session:
            allowed, _ = await try_consume(session, telegram_id, kind,
                                           shadow=_shadow_enabled(shadow))
        return allowed
    except Exception as e:  # never a legitimate block — see _log_failopen
        _log_failopen("quota_gate_failopen", "quota_gate_unexpected_error",
                      e, kind=kind)
        return True


async def within_token_budget(session_factory, telegram_id: int) -> bool:
    """Runaway backstop: is this teacher under their 30-day TOKEN budget?

    Deliberately NOT a quota `kind`. The kinds above count ACTIONS (a variant,
    a check); this counts the tokens those actions actually burned, which is
    what a runaway (a script, a loop, an abusive account) inflates without
    necessarily inflating the action count.

    Reads the recorded gemini_usage rows through usage_summary, scoped to this
    telegram_id, over the same PERIOD the action quotas use. Cheap because
    migration 025 indexes (user_id, created_at).

    settings.USER_TOKEN_BUDGET_30D == 0 DISABLES it entirely (the shipped
    default) and skips the query outright.

    FAILS OPEN on any error, exactly like check_and_consume and can_add_book:
    a quota hiccup must never block a teacher mid-exam. Enable it only after
    watching the cost alerts, so an admin is warned before anyone is blocked.
    """
    budget = settings.USER_TOKEN_BUDGET_30D
    if budget <= 0:
        return True
    try:
        since = _now() - PERIOD
        async with session_factory() as session:
            summary = await usage_summary(session, since, telegram_id)
        used = int(summary.get("prompt_tokens", 0)) + int(summary.get("output_tokens", 0))
        if used >= budget:
            logger.warning(
                "user_token_budget_exceeded",
                telegram_id=telegram_id, used=used, budget=budget,
            )
            return False
        return True
    except Exception as e:  # never a legitimate block — see _log_failopen
        _log_failopen("token_budget_failopen", "token_budget_unexpected_error",
                      e, telegram_id=telegram_id)
        return True
