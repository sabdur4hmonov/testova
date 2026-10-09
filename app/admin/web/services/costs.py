"""
Cost analytics for the admin panel.

Every figure here is costed PER MODEL via usage_log.price_for + estimate_cost
(the Phase-0 fix): rows are grouped by model in SQL and each group is priced at
its own rate, so a table mixing gemini-2.5-flash and gemini-3-flash bills each
correctly. FX (so'm) is applied once from settings.UZS_PER_USD.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.models.gemini_usage import GeminiUsage
from app.models.user import User
from app.services.usage_log import estimate_cost, price_for


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cost_usd(model: str, pin: int, pout: int) -> float:
    pr = price_for(model or "")
    return estimate_cost(pin, pout, 0, pr["in_per_m"], pr["out_per_m"], 1.0)["usd"]


def _som(usd: float) -> float:
    return usd * settings.UZS_PER_USD


async def cost_over_time(session, days: int = 30) -> list[dict]:
    """Daily computed cost for the last `days`, oldest→newest. One point per day
    that has usage. [{date: 'YYYY-MM-DD', usd, som, calls}]."""
    since = _now() - timedelta(days=days)
    day = func.date_trunc("day", GeminiUsage.created_at)
    rows = (await session.execute(
        select(
            day.label("d"),
            GeminiUsage.model,
            func.count().label("n"),
            func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
            func.coalesce(func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0),
        ).where(GeminiUsage.created_at >= since).group_by(day, GeminiUsage.model)
    )).all()

    buckets: dict[str, dict] = {}
    for d, model, n, pin, pout in rows:
        key = d.strftime("%Y-%m-%d")
        b = buckets.setdefault(key, {"date": key, "usd": 0.0, "calls": 0})
        b["usd"] += _cost_usd(model, int(pin), int(pout))
        b["calls"] += int(n)
    out = sorted(buckets.values(), key=lambda x: x["date"])
    for b in out:
        b["som"] = _som(b["usd"])
    return out


async def cost_by_kind(session, days: int = 30) -> list[dict]:
    """Computed cost split by `kind` for the last `days`, most expensive first."""
    since = _now() - timedelta(days=days)
    rows = (await session.execute(
        select(
            GeminiUsage.kind,
            GeminiUsage.model,
            func.count().label("n"),
            func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
            func.coalesce(func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0),
        ).where(GeminiUsage.created_at >= since).group_by(GeminiUsage.kind, GeminiUsage.model)
    )).all()

    agg: dict[str, dict] = {}
    for kind, model, n, pin, pout in rows:
        a = agg.setdefault(kind, {"kind": kind, "usd": 0.0, "calls": 0})
        a["usd"] += _cost_usd(model, int(pin), int(pout))
        a["calls"] += int(n)
    out = sorted(agg.values(), key=lambda x: x["usd"], reverse=True)
    for a in out:
        a["som"] = _som(a["usd"])
    return out


async def per_user_cost(session, days: int = 30, limit: int = 50) -> list[dict]:
    """Top users by computed cost over the last `days`. Rows with NULL user_id
    (unattributed) are folded into a single synthetic entry so the total still
    reconciles with the platform figure."""
    since = _now() - timedelta(days=days)
    rows = (await session.execute(
        select(
            GeminiUsage.user_id,
            GeminiUsage.model,
            func.count().label("n"),
            func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
            func.coalesce(func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0),
        ).where(GeminiUsage.created_at >= since).group_by(GeminiUsage.user_id, GeminiUsage.model)
    )).all()

    agg: dict[int | None, dict] = {}
    for uid, model, n, pin, pout in rows:
        a = agg.setdefault(uid, {"telegram_id": uid, "usd": 0.0, "calls": 0})
        a["usd"] += _cost_usd(model, int(pin), int(pout))
        a["calls"] += int(n)

    # Attach usernames for the real users (one query, not N).
    ids = [uid for uid in agg if uid is not None]
    names: dict[int, tuple[str | None, str]] = {}
    if ids:
        for tg, uname, fname in (await session.execute(
            select(User.telegram_id, User.username, User.full_name)
            .where(User.telegram_id.in_(ids))
        )).all():
            names[tg] = (uname, fname)

    out = []
    for uid, a in agg.items():
        uname, fname = names.get(uid, (None, None)) if uid is not None else (None, None)
        a["username"] = uname
        a["full_name"] = fname if uid is not None else "(nomaʼlum / eski)"
        a["som"] = _som(a["usd"])
        out.append(a)
    out.sort(key=lambda x: x["usd"], reverse=True)
    return out[:limit]


async def window_computed_usd(session, since: datetime, until: datetime | None = None) -> float:
    """Our computed USD cost for [since, until). Used to snapshot the 'ours' side
    of a reconciliation against Google's bill."""
    stmt = select(
        GeminiUsage.model,
        func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
        func.coalesce(func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0),
    ).where(GeminiUsage.created_at >= since)
    if until is not None:
        stmt = stmt.where(GeminiUsage.created_at < until)
    stmt = stmt.group_by(GeminiUsage.model)
    total = 0.0
    for model, pin, pout in (await session.execute(stmt)).all():
        total += _cost_usd(model, int(pin), int(pout))
    return total


async def user_usage_history(session, telegram_id: int, limit: int = 100) -> list[dict]:
    """Most recent gemini_usage rows for one user, each costed by its model."""
    rows = (await session.execute(
        select(
            GeminiUsage.created_at, GeminiUsage.kind, GeminiUsage.model,
            GeminiUsage.prompt_tokens,
            (GeminiUsage.output_tokens + GeminiUsage.thinking_tokens).label("out"),
        ).where(GeminiUsage.user_id == telegram_id)
        .order_by(GeminiUsage.created_at.desc()).limit(limit)
    )).all()
    out = []
    for created_at, kind, model, pin, pout in rows:
        usd = _cost_usd(model, int(pin), int(pout))
        out.append({"created_at": created_at, "kind": kind, "model": model,
                    "prompt_tokens": int(pin), "output_tokens": int(pout),
                    "usd": usd, "som": _som(usd)})
    return out


async def user_cost_by_kind(session, telegram_id: int, days: int = 30) -> list[dict]:
    """One user's cost split by kind over the last `days`."""
    since = _now() - timedelta(days=days)
    rows = (await session.execute(
        select(
            GeminiUsage.kind, GeminiUsage.model, func.count().label("n"),
            func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
            func.coalesce(func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0),
        ).where(GeminiUsage.user_id == telegram_id, GeminiUsage.created_at >= since)
        .group_by(GeminiUsage.kind, GeminiUsage.model)
    )).all()
    agg: dict[str, dict] = {}
    for kind, model, n, pin, pout in rows:
        a = agg.setdefault(kind, {"kind": kind, "usd": 0.0, "calls": 0})
        a["usd"] += _cost_usd(model, int(pin), int(pout))
        a["calls"] += int(n)
    out = sorted(agg.values(), key=lambda x: x["usd"], reverse=True)
    for a in out:
        a["som"] = _som(a["usd"])
    return out


# ── Lifetime totals, for the revenue/profit math ─────────────────────────────
#
# Everything above is WINDOWED (last N days) because the costs page shows
# trends. Profit is not a trend: it is "we collected X from this teacher ever,
# and they have cost us Y ever". Both sides must therefore be lifetime, or the
# subtraction compares a lifetime revenue against a 30-day cost and flatters
# every long-standing customer.
#
# These reuse _cost_usd — the SAME per-model pricing path as every other figure
# in this file (usage_log.price_for + estimate_cost), so a model price change
# lands here too. Nothing about the cost engine is re-implemented.


async def user_cost_total_usd(session, telegram_id: int) -> float:
    """One user's LIFETIME computed Gemini cost, priced per model.

    Counts only rows ATTRIBUTED to this telegram_id. Unattributed rows
    (user_id IS NULL) belong to no teacher and are reported separately by
    attribution_split() — they are not silently spread across users.
    """
    total = 0.0
    for model, pin, pout in (await session.execute(
        select(
            GeminiUsage.model,
            func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
            func.coalesce(func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0),
        ).where(GeminiUsage.user_id == telegram_id).group_by(GeminiUsage.model)
    )).all():
        total += _cost_usd(model, int(pin), int(pout))
    return total


async def cost_total_usd_by_user(session) -> dict[int, float]:
    """LIFETIME cost per telegram_id, for every attributed user, in ONE query.

    The revenue overview needs this for every user at once. Calling
    user_cost_total_usd in a loop would be one round-trip per user — the exact
    shape AUDIT C-24 exists about.
    """
    out: dict[int, float] = {}
    for tg, model, pin, pout in (await session.execute(
        select(
            GeminiUsage.user_id,
            GeminiUsage.model,
            func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
            func.coalesce(func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0),
        ).where(GeminiUsage.user_id.is_not(None))
        .group_by(GeminiUsage.user_id, GeminiUsage.model)
    )).all():
        out[int(tg)] = out.get(int(tg), 0.0) + _cost_usd(model, int(pin), int(pout))
    return out


async def attribution_split(session) -> dict:
    """LIFETIME cost split into what IS attributed to a teacher and what is not.

    This is not a nicety. On the dev database 89% of gemini_usage rows carry a
    NULL user_id — they come from call sites that were never instrumented to
    pass one (see the forward-fix note in AUDIT.md). Any "margin" computed from
    attributed cost alone is therefore optimistic by a large factor, and the
    revenue page has to say so rather than print a flattering number.

    Returns {attributed_usd, unattributed_usd, total_usd, attributed_calls,
    unattributed_calls, attributed_pct}.
    """
    attributed = unattributed = 0.0
    a_calls = u_calls = 0
    for tg_is_null, model, n, pin, pout in (await session.execute(
        select(
            GeminiUsage.user_id.is_(None).label("anon"),
            GeminiUsage.model,
            func.count().label("n"),
            func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
            func.coalesce(func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0),
        ).group_by(GeminiUsage.user_id.is_(None), GeminiUsage.model)
    )).all():
        usd = _cost_usd(model, int(pin), int(pout))
        if tg_is_null:
            unattributed += usd
            u_calls += int(n)
        else:
            attributed += usd
            a_calls += int(n)
    total = attributed + unattributed
    return {
        "attributed_usd": attributed,
        "unattributed_usd": unattributed,
        "total_usd": total,
        "attributed_calls": a_calls,
        "unattributed_calls": u_calls,
        "attributed_pct": (100.0 * a_calls / (a_calls + u_calls)) if (a_calls + u_calls) else 0.0,
    }
