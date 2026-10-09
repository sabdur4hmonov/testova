"""
Unit tests for the admin panel's service layer: costs, balance, alerts,
book_usage.

WHY THE ARITHMETIC IS PINNED. Every number the panel shows — the dashboard
tile, the costs chart, per-user spend, the Gemini balance, the alert
thresholds, the reconciliation delta — is `price_for` × `estimate_cost` over
`gemini_usage` rows. A wrong price map or a lost per-model GROUP BY does not
crash anything: it renders a plausible, wrong dollar figure, and the only way
to notice is to reconcile against Google's bill by hand. So the prices and the
grouping are asserted against hand-computed values rather than against
whatever the code currently returns.

The model-mixing guard is the important one. Both models in the real price map
are currently priced identically (gemini-3-flash carries a placeholder copied
from 2.5-flash), so a bug that flattened every row to one rate would be
invisible against live config. Those tests patch in two models with
deliberately different prices, which is what makes the grouping observable.
"""
from __future__ import annotations

import contextlib
import importlib
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 — registers EVERY table on Base.metadata
from app.admin.web.services import (alerts, balance, book_usage, costs, moderation,
                                    monitoring, reconciliation, revenue)
from app.config import settings
from app.models.billing_reconciliation import BillingReconciliation
from app.models.book import Book, BookTheme
from app.models.credit_baseline import CreditBaseline
from app.models.gemini_usage import GeminiUsage
from app.models.user import User
from app.services.usage_log import estimate_cost, price_for

TG_A = 200_001
TG_B = 200_002

# Two models priced far enough apart that a flat-rate bug cannot hide behind
# rounding. CHEAP bills $1 per Mtok in; DEAR bills $100.
CHEAP = "test-model-cheap"
DEAR = "test-model-dear"
FAKE_PRICING = {
    CHEAP: {"in_per_m": 1.0, "out_per_m": 10.0, "verified": "2026-01-01"},
    DEAR: {"in_per_m": 100.0, "out_per_m": 1000.0, "verified": "2026-01-01"},
    # price_for falls back to GEMINI_PRICING[GEMINI_PRICING_FALLBACK] by name,
    # so the fallback key must exist in any map we patch in.
    "gemini-2.5-flash": {"in_per_m": 0.30, "out_per_m": 2.50, "verified": "2026-08-22"},
}


@pytest.fixture
def fake_prices(monkeypatch):
    """Swap the price map. `price_for` imports it inside the function body, so
    patching the module attribute is enough — no reload needed."""
    monkeypatch.setattr("app.config.GEMINI_PRICING", FAKE_PRICING)
    monkeypatch.setattr("app.config.GEMINI_PRICING_FALLBACK", "gemini-2.5-flash")


def _usage(**kw) -> GeminiUsage:
    kw.setdefault("kind", "extract")
    kw.setdefault("prompt_tokens", 0)
    kw.setdefault("output_tokens", 0)
    kw.setdefault("thinking_tokens", 0)
    kw.setdefault("total_tokens", kw["prompt_tokens"] + kw["output_tokens"]
                  + kw["thinking_tokens"])
    return GeminiUsage(**kw)


# ── pricing arithmetic ───────────────────────────────────────────────────────

def test_estimate_cost_on_hand_computed_numbers():
    # 2M prompt @ $0.30/M = $0.60; 1M output @ $2.50/M = $2.50.
    c = estimate_cost(2_000_000, 1_000_000, 0, 0.30, 2.50, 12_000.0)
    assert c["input_usd"] == pytest.approx(0.60)
    assert c["output_usd"] == pytest.approx(2.50)
    assert c["usd"] == pytest.approx(3.10)
    assert c["som"] == pytest.approx(3.10 * 12_000.0)


def test_thinking_tokens_are_billed_at_the_output_rate():
    """Not a detail: thinking tokens dominate spend on reasoning models, and
    billing them as input would understate the bill ~8x at these prices."""
    as_output = estimate_cost(0, 1_000_000, 0, 0.30, 2.50, 1.0)["usd"]
    as_thinking = estimate_cost(0, 0, 1_000_000, 0.30, 2.50, 1.0)["usd"]
    assert as_thinking == pytest.approx(as_output) == pytest.approx(2.50)


def test_price_for_a_known_model_is_marked_priced():
    p = price_for("gemini-2.5-flash")
    assert p["priced"] is True
    assert p["in_per_m"] == 0.30 and p["out_per_m"] == 2.50


def test_price_for_an_unknown_model_falls_back_and_flags_it():
    """An unpriced model must still cost SOMETHING — a dropped row would make
    real spend vanish from the panel — but it has to be flagged, not silently
    billed at a neighbour's rate."""
    p = price_for("gemini-99-ultra-does-not-exist")
    assert p["priced"] is False
    assert p["in_per_m"] > 0 and p["out_per_m"] > 0


def test_configured_prices_are_the_ones_the_panel_was_reconciled_against():
    """TRIPWIRE, not a restatement of config.

    Every stored cost figure and every Google reconciliation on record was
    computed at these rates. Changing them silently re-prices history. If this
    test fails, that is the intended signal: update it deliberately, and expect
    the reconciliation deltas to move.
    """
    from app.config import GEMINI_PRICING, GEMINI_PRICING_FALLBACK

    assert GEMINI_PRICING["gemini-2.5-flash"] == {
        "in_per_m": 0.30, "out_per_m": 2.50, "verified": "2026-08-22",
    }
    assert GEMINI_PRICING_FALLBACK in GEMINI_PRICING, (
        "the fallback model must exist in the map or price_for raises KeyError"
    )
    for model, entry in GEMINI_PRICING.items():
        assert entry["in_per_m"] > 0 and entry["out_per_m"] > 0, model


# ── per-model grouping (the model-mixing guard) ──────────────────────────────

async def test_cost_by_kind_prices_each_model_at_its_own_rate(
    session: AsyncSession, fake_prices
):
    """THE regression guard for flat-rate costing.

    One kind, two models, 1M prompt tokens each. Correct: $1 + $100 = $101.
    A single-rate bug gives $2 (all cheap) or $200 (all dear).
    """
    now = datetime.now(timezone.utc)
    session.add_all([
        _usage(user_id=TG_A, kind="extract", model=CHEAP,
               prompt_tokens=1_000_000, created_at=now),
        _usage(user_id=TG_A, kind="extract", model=DEAR,
               prompt_tokens=1_000_000, created_at=now),
    ])
    await session.flush()

    rows = await costs.cost_by_kind(session, days=30)
    assert len(rows) == 1
    assert rows[0]["kind"] == "extract"
    assert rows[0]["calls"] == 2
    assert rows[0]["usd"] == pytest.approx(101.0)
    assert rows[0]["som"] == pytest.approx(101.0 * settings.UZS_PER_USD)


async def test_per_user_cost_prices_each_model_at_its_own_rate(
    session: AsyncSession, fake_prices
):
    now = datetime.now(timezone.utc)
    session.add_all([
        User(telegram_id=TG_A, username="a", full_name="User A"),
        User(telegram_id=TG_B, username="b", full_name="User B"),
    ])
    session.add_all([
        _usage(user_id=TG_A, model=CHEAP, prompt_tokens=1_000_000, created_at=now),
        _usage(user_id=TG_A, model=DEAR, prompt_tokens=1_000_000, created_at=now),
        _usage(user_id=TG_B, model=CHEAP, prompt_tokens=2_000_000, created_at=now),
    ])
    await session.flush()

    rows = await costs.per_user_cost(session, days=30)
    by_id = {r["telegram_id"]: r for r in rows}
    assert by_id[TG_A]["usd"] == pytest.approx(101.0)
    assert by_id[TG_B]["usd"] == pytest.approx(2.0)
    # most expensive first
    assert rows[0]["telegram_id"] == TG_A
    assert by_id[TG_A]["username"] == "a"


async def test_per_user_cost_folds_unattributed_rows_into_one_entry(
    session: AsyncSession, fake_prices
):
    """478 historical rows carry a NULL user_id. They must still appear in the
    total — dropping them would make the per-user table silently disagree with
    the platform figure — as a single synthetic row, not one per call."""
    now = datetime.now(timezone.utc)
    session.add_all([
        _usage(user_id=None, model=CHEAP, prompt_tokens=1_000_000, created_at=now),
        _usage(user_id=None, model=CHEAP, prompt_tokens=1_000_000, created_at=now),
    ])
    await session.flush()

    rows = await costs.per_user_cost(session, days=30)
    orphan = [r for r in rows if r["telegram_id"] is None]
    assert len(orphan) == 1, "unattributed rows must fold into exactly one entry"
    assert orphan[0]["calls"] == 2
    assert orphan[0]["usd"] == pytest.approx(2.0)


async def test_cost_over_time_buckets_by_day_and_sorts_oldest_first(
    session: AsyncSession, fake_prices
):
    now = datetime.now(timezone.utc)
    session.add_all([
        _usage(model=CHEAP, prompt_tokens=1_000_000, created_at=now),
        _usage(model=CHEAP, prompt_tokens=1_000_000, created_at=now - timedelta(days=2)),
        _usage(model=DEAR, prompt_tokens=1_000_000, created_at=now - timedelta(days=2)),
    ])
    await session.flush()

    rows = await costs.cost_over_time(session, days=30)
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    assert len(rows) == 2
    assert rows[-1]["usd"] == pytest.approx(1.0)     # today: cheap only
    assert rows[0]["usd"] == pytest.approx(101.0)    # two days ago: both models
    assert rows[0]["calls"] == 2


async def test_cost_over_time_ignores_rows_outside_the_window(
    session: AsyncSession, fake_prices
):
    now = datetime.now(timezone.utc)
    session.add(_usage(model=DEAR, prompt_tokens=1_000_000,
                       created_at=now - timedelta(days=45)))
    await session.flush()
    assert await costs.cost_over_time(session, days=30) == []


async def test_window_computed_usd_is_half_open_on_until(
    session: AsyncSession, fake_prices
):
    """[since, until) — an inclusive `until` would double-count the boundary row
    across two consecutive reconciliation windows."""
    base = datetime.now(timezone.utc) - timedelta(days=10)
    session.add_all([
        _usage(model=CHEAP, prompt_tokens=1_000_000, created_at=base),
        _usage(model=CHEAP, prompt_tokens=1_000_000, created_at=base + timedelta(hours=1)),
    ])
    await session.flush()

    both = await costs.window_computed_usd(session, base)
    assert both == pytest.approx(2.0)
    # `until` set exactly on the second row's timestamp must exclude it
    first_only = await costs.window_computed_usd(
        session, base, base + timedelta(hours=1))
    assert first_only == pytest.approx(1.0)


async def test_user_cost_by_kind_is_scoped_to_one_user(
    session: AsyncSession, fake_prices
):
    now = datetime.now(timezone.utc)
    session.add_all([
        _usage(user_id=TG_A, kind="extract", model=CHEAP,
               prompt_tokens=1_000_000, created_at=now),
        _usage(user_id=TG_B, kind="extract", model=DEAR,
               prompt_tokens=1_000_000, created_at=now),
    ])
    await session.flush()

    rows = await costs.user_cost_by_kind(session, TG_A, days=30)
    assert len(rows) == 1
    assert rows[0]["usd"] == pytest.approx(1.0), "another user's spend leaked in"


# ── balance ──────────────────────────────────────────────────────────────────

async def test_balance_is_none_until_a_baseline_is_set(session: AsyncSession):
    assert await balance.status(session) == {"baseline": None}


async def test_balance_counts_only_usage_after_as_of(
    session: AsyncSession, fake_prices
):
    """Spend BEFORE the baseline was anchored is already reflected in the credit
    the admin typed in. Counting it again would show the balance draining twice
    as fast as it really is."""
    now = datetime.now(timezone.utc)
    as_of = now - timedelta(days=1)
    session.add(CreditBaseline(credit_usd=50.0, as_of=as_of))
    session.add_all([
        _usage(model=CHEAP, prompt_tokens=1_000_000, created_at=as_of - timedelta(days=5)),
        _usage(model=CHEAP, prompt_tokens=1_000_000, created_at=now),
    ])
    await session.flush()

    st = await balance.status(session)
    assert st["spent_usd"] == pytest.approx(1.0), "pre-baseline spend was counted"
    assert st["remaining_usd"] == pytest.approx(49.0)
    assert st["remaining_som"] == pytest.approx(49.0 * settings.UZS_PER_USD)


async def test_balance_uses_the_most_recent_baseline(
    session: AsyncSession, fake_prices
):
    """Baselines are append-only; re-anchoring after a reconciliation must take
    effect, so the newest row wins rather than the first."""
    now = datetime.now(timezone.utc)
    session.add(CreditBaseline(credit_usd=10.0, as_of=now - timedelta(days=10),
                               created_at=now - timedelta(days=10)))
    session.add(CreditBaseline(credit_usd=99.0, as_of=now - timedelta(days=1),
                               created_at=now - timedelta(days=1)))
    await session.flush()

    st = await balance.status(session)
    assert st["baseline"].credit_usd == pytest.approx(99.0)


# ── reconciliation ───────────────────────────────────────────────────────────

async def test_reconciliation_records_google_minus_ours(
    session: AsyncSession, fake_prices
):
    now = datetime.now(timezone.utc)
    session.add(_usage(model=CHEAP, prompt_tokens=1_000_000,
                       created_at=now - timedelta(days=2)))
    await session.flush()

    row = await reconciliation.record(
        session, period_label="2026-09", google_total_usd=1.5,
        since=now - timedelta(days=5),
    )
    assert row.our_computed_usd == pytest.approx(1.0)
    assert row.delta_usd == pytest.approx(0.5)
    assert row.delta_pct == pytest.approx(50.0)


async def test_reconciliation_does_not_divide_by_zero_on_no_usage(
    session: AsyncSession, fake_prices
):
    """Google billed something, we recorded nothing — the most alarming case,
    and the one where a naive delta/ours raises instead of reporting."""
    row = await reconciliation.record(
        session, period_label="empty", google_total_usd=7.0)
    assert row.our_computed_usd == pytest.approx(0.0)
    assert row.delta_usd == pytest.approx(7.0)
    assert row.delta_pct == 0.0


# ── alerts ───────────────────────────────────────────────────────────────────

async def test_no_alerts_when_everything_is_quiet(session: AsyncSession, fake_prices):
    ev = await alerts.evaluate(session)
    assert ev["active_count"] == 0
    assert ev["daily"]["breached"] is False
    assert ev["user_alerts"] == []
    assert ev["recon_alerts"] == []


async def test_daily_alert_breaches_at_the_threshold(
    session: AsyncSession, fake_prices, monkeypatch
):
    """`>=`, not `>`: spend landing exactly on the threshold must alert."""
    monkeypatch.setattr(settings, "ALERT_DAILY_USD", 1.0)
    now = datetime.now(timezone.utc)
    session.add(_usage(model=CHEAP, prompt_tokens=1_000_000, created_at=now))
    await session.flush()

    ev = await alerts.evaluate(session)
    assert ev["daily"]["spend_usd"] == pytest.approx(1.0)
    assert ev["daily"]["breached"] is True
    assert ev["active_count"] == 1


async def test_daily_alert_ignores_yesterdays_spend(
    session: AsyncSession, fake_prices, monkeypatch
):
    monkeypatch.setattr(settings, "ALERT_DAILY_USD", 1.0)
    now = datetime.now(timezone.utc)
    session.add(_usage(model=DEAR, prompt_tokens=1_000_000,
                       created_at=now - timedelta(days=2)))
    await session.flush()

    ev = await alerts.evaluate(session)
    assert ev["daily"]["breached"] is False, "a previous day's spend raised today's alert"


async def test_user_alert_lists_only_users_over_the_threshold(
    session: AsyncSession, fake_prices, monkeypatch
):
    monkeypatch.setattr(settings, "ALERT_DAILY_USD", 10_000.0)  # keep daily quiet
    monkeypatch.setattr(settings, "ALERT_USER_USD_30D", 50.0)
    now = datetime.now(timezone.utc)
    session.add_all([
        User(telegram_id=TG_A, username="spender", full_name="Big Spender"),
        User(telegram_id=TG_B, username="thrifty", full_name="Small Spender"),
    ])
    session.add_all([
        _usage(user_id=TG_A, model=DEAR, prompt_tokens=1_000_000, created_at=now),
        _usage(user_id=TG_B, model=CHEAP, prompt_tokens=1_000_000, created_at=now),
    ])
    await session.flush()

    ev = await alerts.evaluate(session)
    assert [a["telegram_id"] for a in ev["user_alerts"]] == [TG_A]
    assert ev["user_alerts"][0]["username"] == "spender"


async def test_unattributed_spend_never_raises_a_per_user_alert(
    session: AsyncSession, fake_prices, monkeypatch
):
    """NULL user_id rows belong to no teacher; naming them in a per-user alert
    would send the admin chasing a user that does not exist."""
    monkeypatch.setattr(settings, "ALERT_DAILY_USD", 10_000.0)
    monkeypatch.setattr(settings, "ALERT_USER_USD_30D", 1.0)
    now = datetime.now(timezone.utc)
    session.add(_usage(user_id=None, model=DEAR, prompt_tokens=1_000_000, created_at=now))
    await session.flush()

    ev = await alerts.evaluate(session)
    assert ev["user_alerts"] == []


async def test_recon_alert_fires_only_above_tolerance(
    session: AsyncSession, fake_prices, monkeypatch
):
    monkeypatch.setattr(settings, "ALERT_DAILY_USD", 10_000.0)
    monkeypatch.setattr(settings, "RECON_TOLERANCE_PCT", 10.0)
    session.add_all([
        BillingReconciliation(period_label="within", google_total_usd=1.05,
                              our_computed_usd=1.0, delta_usd=0.05, delta_pct=5.0),
        BillingReconciliation(period_label="over", google_total_usd=1.5,
                              our_computed_usd=1.0, delta_usd=0.5, delta_pct=50.0),
    ])
    await session.flush()

    ev = await alerts.evaluate(session)
    labels = [r.period_label for r in ev["recon_alerts"]]
    assert labels == ["over"], "tolerance boundary is wrong"


# ── reconciliation severity (CP-1b) ─────────────────────────────────────────
#
# delta_pct is delta/ours and is stored as 0.0 when ours is $0, so the alert
# filter `delta_pct > tolerance` could never fire for "we recorded nothing,
# Google charged us" — the uninstrumented-spend case the guard exists for. And
# a percentage alone misses a large absolute gap on a large bill.

def _recon(label, google, ours):
    delta = google - ours
    return BillingReconciliation(period_label=label, google_total_usd=google,
                                 our_computed_usd=ours, delta_usd=delta,
                                 delta_pct=(delta / ours * 100.0) if ours else 0.0)


@pytest.fixture
def recon_thresholds(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_DAILY_USD", 10_000.0)  # keep daily quiet
    monkeypatch.setattr(settings, "RECON_TOLERANCE_PCT", 10.0)
    monkeypatch.setattr(settings, "RECON_TOLERANCE_USD", 1.0, raising=False)


def test_recon_tolerance_usd_defaults_to_one_dollar():
    from app.config import Settings
    assert Settings.model_fields["RECON_TOLERANCE_USD"].default == 1.0


async def test_google_billing_while_we_recorded_nothing_is_a_critical_alert(
    session: AsyncSession, fake_prices, recon_thresholds
):
    blind = _recon("blind", google=7.0, ours=0.0)
    session.add(blind)
    await session.flush()

    assert blind.delta_pct == 0.0            # stored as before — no migration
    assert reconciliation.severity(blind) == "critical"
    ev = await alerts.evaluate(session)
    assert [r.period_label for r in ev["recon_alerts"]] == ["blind"]
    assert ev["recon_severity"][str(blind.id)] == "critical"
    assert ev["active_count"] == 1


async def test_nothing_billed_and_nothing_recorded_is_quiet(
    session: AsyncSession, fake_prices, recon_thresholds
):
    """Control: $0 vs $0 is a quiet month, not a critical one."""
    session.add(_recon("idle", google=0.0, ours=0.0))
    await session.flush()
    ev = await alerts.evaluate(session)
    assert ev["recon_alerts"] == []


async def test_a_large_dollar_gap_under_the_percentage_still_alerts(
    session: AsyncSession, fake_prices, recon_thresholds
):
    """8% of a $100 bill is $8 unaccounted for — under the 10% line, over $1."""
    big = _recon("big-bill", google=108.0, ours=100.0)
    session.add(big)
    await session.flush()
    assert reconciliation.severity(big) == "warning"
    ev = await alerts.evaluate(session)
    assert ev["recon_severity"] == {str(big.id): "warning"}


async def test_the_dollar_threshold_is_strictly_greater_like_the_percentage(
    session: AsyncSession, fake_prices, recon_thresholds
):
    at = _recon("at-line", google=101.0, ours=100.0)      # exactly $1, 1%
    over = _recon("over-line", google=101.5, ours=100.0)  # $1.50, 1.5%
    session.add_all([at, over])
    await session.flush()
    ev = await alerts.evaluate(session)
    assert [r.period_label for r in ev["recon_alerts"]] == ["over-line"]


async def test_google_below_ours_never_alerts(
    session: AsyncSession, fake_prices, recon_thresholds
):
    """Control: over-counting our own spend is not uninstrumented spend."""
    session.add(_recon("overcount", google=50.0, ours=100.0))
    await session.flush()
    ev = await alerts.evaluate(session)
    assert ev["recon_alerts"] == []


async def test_the_notifier_names_the_recorded_nothing_case(
    session: AsyncSession, fake_prices, recon_thresholds
):
    from app.tasks import alert_check

    session.add(_recon("blind", google=7.0, ours=0.0))
    await session.flush()
    text = alert_check._format(await alerts.evaluate(session))
    assert text is not None
    assert "+0.0%" not in text, "the critical case still reads as a zero-percent gap"
    assert "$7.00" in text and "$0" in text, text


# ── book_usage ───────────────────────────────────────────────────────────────

def test_ceiling_status_unlimited_when_no_limit_is_set():
    assert book_usage._ceiling_status(99, None) == "unlimited"


def test_ceiling_status_at_and_over_the_cap():
    assert book_usage._ceiling_status(4, 4) == "at"
    assert book_usage._ceiling_status(5, 4) == "at", "over the cap must not read as 'near'"


def test_ceiling_status_near_is_within_20_percent_but_at_least_one_slot():
    # limit 4 -> ceil(0.8) = 1, floored at 1: only the last slot is "near".
    assert book_usage._ceiling_status(3, 4) == "near"
    assert book_usage._ceiling_status(2, 4) == "ok"
    # limit 10 -> ceil(2.0) = 2: the last two slots are "near".
    assert book_usage._ceiling_status(8, 10) == "near"
    assert book_usage._ceiling_status(7, 10) == "ok"
    # a cap of 1 still leaves room for a "near" band of one slot
    assert book_usage._ceiling_status(0, 1) == "near"


def test_ceiling_status_near_band_matches_the_documented_formula():
    """Pins the band itself, so a tweak to the 20% constant is a deliberate act
    rather than a quiet shift in who shows up flagged on the books page."""
    for limit in range(1, 30):
        band = max(1, math.ceil(0.2 * limit))
        assert book_usage._ceiling_status(limit - band, limit) == "near"
        if limit - band - 1 >= 0:
            assert book_usage._ceiling_status(limit - band - 1, limit) == "ok"


async def test_book_usage_counts_owned_books_against_the_ceiling(session: AsyncSession):
    owner = User(telegram_id=TG_A, username="owner", full_name="Owner",
                 book_limit=2)
    session.add(owner)
    await session.flush()
    session.add_all([
        Book(user_id=owner.id, title="One", original_pdf_path="a.pdf",
             pdf_checksum="aa", uploaded_by_user_id=TG_A),
        Book(user_id=owner.id, title="Two", original_pdf_path="b.pdf",
             pdf_checksum="bb", uploaded_by_user_id=TG_A),
    ])
    await session.flush()

    rows = await book_usage.book_usage_rows(session)
    row = next(r for r in rows if r["telegram_id"] == TG_A)
    assert row["books_owned"] == 2
    assert row["book_limit"] == 2
    assert row["status"] == "at"


async def test_book_usage_counts_only_analysis_kinds(session: AsyncSession):
    """book_extract and book_generate are not analysis passes. Counting them
    would inflate the lifetime figure the book-limit conversation is had over."""
    owner = User(telegram_id=TG_A, username="owner", full_name="Owner", book_limit=5)
    session.add(owner)
    now = datetime.now(timezone.utc)
    session.add_all([
        _usage(user_id=TG_A, kind="book_toc", model="gemini-2.5-flash", created_at=now),
        _usage(user_id=TG_A, kind="book_toc_photo", model="gemini-2.5-flash", created_at=now),
        _usage(user_id=TG_A, kind="book_extract", model="gemini-2.5-flash", created_at=now),
        _usage(user_id=TG_A, kind="book_generate", model="gemini-2.5-flash", created_at=now),
    ])
    await session.flush()

    rows = await book_usage.book_usage_rows(session)
    row = next(r for r in rows if r["telegram_id"] == TG_A)
    assert row["analysis_total"] == 2


async def test_analysis_total_stays_lifetime_and_is_not_windowed(session: AsyncSession):
    """THE guard on the C-27 decision.

    C-27 proposed bounding this aggregate to a recent time window to kill the
    sequential scan. That was rejected and an index used instead, because the
    value is rendered under the header "Tahlil (jami)" ("Analysis (total)")
    directly beside the 30-day meter "Tahlil (oy)" — windowing it would have
    made the two columns duplicate each other and the header wrong.

    So: a pass from well over a year ago must still be counted. If someone
    later adds a `created_at >= since` filter for performance, this fails and
    says why.
    """
    owner = User(telegram_id=TG_A, username="owner", full_name="Owner", book_limit=5)
    session.add(owner)
    now = datetime.now(timezone.utc)
    session.add_all([
        _usage(user_id=TG_A, kind="book_toc", model="gemini-2.5-flash", created_at=now),
        _usage(user_id=TG_A, kind="book_toc", model="gemini-2.5-flash",
               created_at=now - timedelta(days=400)),
    ])
    await session.flush()

    rows = await book_usage.book_usage_rows(session)
    row = next(r for r in rows if r["telegram_id"] == TG_A)
    assert row["analysis_total"] == 2, (
        "analysis_total dropped a historical pass — it is a LIFETIME count, "
        "not a windowed one"
    )


def test_gemini_usage_declares_the_kind_index_migration_031_creates():
    """Model and migration must not drift: the ORM's metadata is what
    create_all() builds in tests, the migration is what production gets."""
    from app.models.gemini_usage import GeminiUsage

    names = {ix.name for ix in GeminiUsage.__table__.indexes}
    assert "ix_gemini_usage_kind_user_id" in names, names


async def test_book_usage_ignores_unattributed_analysis_rows(session: AsyncSession):
    owner = User(telegram_id=TG_A, username="owner", full_name="Owner", book_limit=5)
    session.add(owner)
    now = datetime.now(timezone.utc)
    session.add_all([
        _usage(user_id=None, kind="book_toc", model="gemini-2.5-flash", created_at=now),
        _usage(user_id=TG_A, kind="book_toc", model="gemini-2.5-flash", created_at=now),
    ])
    await session.flush()

    rows = await book_usage.book_usage_rows(session)
    row = next(r for r in rows if r["telegram_id"] == TG_A)
    assert row["analysis_total"] == 1, "a NULL-user analysis row was attributed"


# ── admin_log actions: the static dropdown list must not drift ───────────────

def test_admin_log_actions_covers_every_action_the_code_actually_writes():
    """THE guard that makes hardcoding the dropdown safe (AUDIT C-26).

    The audit page's action filter used to be rebuilt by a full-table
    `SELECT DISTINCT action` on every render. It is now a fixed tuple — which
    is only correct while it matches reality, and a new action added to any
    write site would otherwise vanish from the dropdown silently.

    So: scan the sources that construct AdminLog rows and assert the constant
    covers what they write. Deliberately NOT asserted against the database —
    the dev DB held only 8 of these 18, because the others simply had not
    happened yet, which is exactly the blind spot this replaces.
    """
    from app.models.admin_log import ADMIN_LOG_ACTIONS

    repo_root = Path(__file__).resolve().parents[1]
    sources = [
        repo_root / "app" / "services" / "admin_users.py",
        repo_root / "app" / "services" / "broadcast.py",
        repo_root / "app" / "admin" / "web" / "services" / "moderation.py",
        # The payment ledger writes an admin_log row too ("payment"). Added
        # here when revenue.py landed — a write site missing from this list is
        # a hole in the guard, not a passing test.
        repo_root / "app" / "admin" / "web" / "services" / "revenue.py",
    ]

    found: set[str] = set()
    for src in sources:
        text = src.read_text(encoding="utf-8")
        # `action="book_delete"` / `action="broadcast"` keyword form
        found |= set(re.findall(r'action=["\']([a-z_]+)["\']', text))
        # `_write_log(session, admin_id, "grant", ...)` positional form
        found |= set(re.findall(r'_write_log\(\s*session,\s*admin_id,\s*["\']([a-z_]+)["\']', text))
        # the ternary at admin_users.py:284 — "revoke" if blocked else "unblock"
        found |= set(re.findall(r'["\']([a-z_]+)["\']\s+if\s+blocked\s+else\s+["\']([a-z_]+)["\']', text)[0]
                     if re.search(r'if\s+blocked\s+else', text) else [])

    missing = found - set(ADMIN_LOG_ACTIONS)
    assert not missing, (
        f"actions written by the code but missing from ADMIN_LOG_ACTIONS: "
        f"{sorted(missing)} — add them or they vanish from the audit filter"
    )
    # Sanity: the scan has to actually be finding things, or this test passes
    # vacuously forever.
    assert len(found) >= 15, f"the source scan only found {len(found)} actions"


def test_admin_log_actions_is_sorted_and_unique():
    """The removed query was `ORDER BY action`, so the dropdown was
    alphabetical; keep it that way and keep it duplicate-free."""
    from app.models.admin_log import ADMIN_LOG_ACTIONS

    assert list(ADMIN_LOG_ACTIONS) == sorted(ADMIN_LOG_ACTIONS)
    assert len(set(ADMIN_LOG_ACTIONS)) == len(ADMIN_LOG_ACTIONS)


# ── monitoring.LOG_FILE: CWD-independence ────────────────────────────────────

def test_log_file_is_anchored_to_the_repo_root_not_cwd():
    """LOG_FILE used to be Path("logs/testova.log") — resolved against the
    process's CWD. Starting the panel from anywhere but the repo root (a
    systemd unit's own WorkingDirectory, a terminal cd'd elsewhere) made
    /alerts silently report "no log" — no error, just an empty error page.

    Proven by actually changing CWD and reloading the module, not by reading
    the formula: a CWD-independent path looks IDENTICAL to a CWD-relative one
    when the test happens to run from the repo root — which is exactly how
    this bug hid in the first place.
    """
    expected = Path(monitoring.__file__).resolve().parents[4] / "logs" / "testova.log"
    assert monitoring.LOG_FILE == expected
    assert monitoring.LOG_FILE.is_absolute()

    # On Windows, TemporaryDirectory's own cleanup fails if CWD is still
    # pointed AT the directory being removed ("used by another process") — so
    # chdir back out BEFORE the `with` block exits, not after, in the finally.
    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as elsewhere:
        try:
            os.chdir(elsewhere)
            importlib.reload(monitoring)
            assert monitoring.LOG_FILE == expected, (
                "LOG_FILE moved when the process's CWD changed"
            )
        finally:
            os.chdir(original_cwd)
    importlib.reload(monitoring)  # restore normal state for tests that follow


# ── monitoring: TTL cache + off-the-loop read (AUDIT C-29) ───────────────────

def _write_log(path: Path, events: list[tuple[str, str]]) -> None:
    """Write a structlog-shaped JSON log: (level, event) per line, stamped now."""
    now = datetime.now(timezone.utc).isoformat()
    path.write_text(
        "\n".join(
            f'{{"level": "{lvl}", "event": "{ev}", "logger": "app.test", '
            f'"timestamp": "{now}"}}'
            for lvl, ev in events
        ),
        encoding="utf-8",
    )


async def test_error_summary_cached_reads_the_log_and_counts_errors(monkeypatch, tmp_path):
    log = tmp_path / "testova.log"
    _write_log(log, [("error", "gemini boom"), ("warning", "timeout here"),
                     ("info", "not an error")])
    monkeypatch.setattr(monitoring, "LOG_FILE", log)
    monitoring.reset_cache()

    summary = await monitoring.error_summary_cached()
    assert summary["log_available"] is True
    assert summary["total_24h"] == 2, "info lines must not count as errors"


async def test_error_summary_cached_serves_the_cache_within_the_ttl(monkeypatch, tmp_path):
    """THE point of C-29: a second page load must not re-read and re-parse the
    log. Proven by changing the file underneath and showing the answer does
    not move — if the read had happened again, the count would."""
    log = tmp_path / "testova.log"
    _write_log(log, [("error", "first")])
    monkeypatch.setattr(monitoring, "LOG_FILE", log)
    monitoring.reset_cache()

    first = await monitoring.error_summary_cached()
    assert first["total_24h"] == 1

    _write_log(log, [("error", "first"), ("error", "second"), ("error", "third")])
    second = await monitoring.error_summary_cached()
    assert second["total_24h"] == 1, "the log was re-read inside the TTL"
    assert second is first, "a cache hit should return the same object"


async def test_error_summary_cache_expires_after_the_ttl(monkeypatch, tmp_path):
    """The cache must be a TTL, not a permanent freeze — an operator watching
    an incident needs the numbers to move."""
    log = tmp_path / "testova.log"
    _write_log(log, [("error", "first")])
    monkeypatch.setattr(monitoring, "LOG_FILE", log)
    monitoring.reset_cache()

    assert (await monitoring.error_summary_cached())["total_24h"] == 1

    _write_log(log, [("error", "first"), ("error", "second")])
    # Rewind the stored timestamp past the TTL rather than sleeping 60s.
    stamped_at, summary = monitoring._cache
    monitoring._cache = (stamped_at - monitoring.CACHE_TTL_SECONDS - 1, summary)

    assert (await monitoring.error_summary_cached())["total_24h"] == 2


async def test_reset_cache_forces_a_fresh_read(monkeypatch, tmp_path):
    log = tmp_path / "testova.log"
    _write_log(log, [("error", "first")])
    monkeypatch.setattr(monitoring, "LOG_FILE", log)
    monitoring.reset_cache()

    assert (await monitoring.error_summary_cached())["total_24h"] == 1
    _write_log(log, [("error", "first"), ("error", "second")])
    monitoring.reset_cache()
    assert (await monitoring.error_summary_cached())["total_24h"] == 2


async def test_an_explicit_now_neither_reads_nor_poisons_the_cache(monkeypatch, tmp_path):
    """A caller pinning the clock wants a deterministic answer for that
    instant; serving it a wall-time cache entry — or storing its answer for
    everyone else — would both be wrong."""
    log = tmp_path / "testova.log"
    _write_log(log, [("error", "first")])
    monkeypatch.setattr(monitoring, "LOG_FILE", log)
    monitoring.reset_cache()

    # A pinned time far in the future: every log line is older than the 7d
    # window, so nothing is counted.
    future = datetime.now(timezone.utc) + timedelta(days=400)
    pinned = await monitoring.error_summary_cached(now=future)
    assert pinned["total_7d"] == 0
    assert monitoring._cache is None, "an explicit `now` must not populate the cache"

    # And it must not have been served a cached value either: prime the cache,
    # then ask again with the pinned clock and check it still reflects `now`.
    live = await monitoring.error_summary_cached()
    assert live["total_24h"] == 1
    again = await monitoring.error_summary_cached(now=future)
    assert again["total_7d"] == 0, "a pinned clock was served the wall-time cache"


async def test_error_summary_cached_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """The read must happen off the loop (asyncio.to_thread), so other
    coroutines keep running while a multi-MB log is parsed.

    Asserted by having the read itself block for a beat and checking a
    concurrent ticker still got scheduled during it — a to_thread call yields
    to the loop, a direct synchronous call would not.
    """
    import asyncio

    monkeypatch.setattr(monitoring, "LOG_FILE", tmp_path / "absent.log")
    monitoring.reset_cache()

    real = monitoring.error_summary

    def _slow(now=None):
        time.sleep(0.2)  # stand-in for the 4 MB read + per-line json.loads
        return real(now)

    monkeypatch.setattr(monitoring, "error_summary", _slow)

    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(_ticker())
    await monitoring.error_summary_cached()
    task.cancel()

    assert ticks > 0, (
        "the event loop was blocked for the whole read — error_summary is "
        "being called directly instead of via asyncio.to_thread"
    )


# ── moderation listing: what it must NOT pull (AUDIT C-33) ───────────────────

class _SQLCapture:
    """Every statement actually sent to the DB, so a test can assert on the SQL
    itself rather than on timings (noise at this data size)."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    @property
    def selects(self) -> list[str]:
        return [s for s in self.statements if s.strip().upper().startswith("SELECT")]


@contextlib.contextmanager
def _capture_sql(session: AsyncSession):
    """Capture via the event API, NOT by raising the engine logger's level.

    The logging route silently captures NOTHING here: SQLAlchemy resolves
    echo once per Connection (`Connection._echo`), and the fixture's session
    has already opened its connection by the time a test runs — so the level
    change comes too late and every assertion below would pass vacuously
    against an empty list. before_cursor_execute has no such timing
    dependency.
    """
    cap = _SQLCapture()
    engine = session.get_bind()
    target = getattr(engine, "sync_engine", engine)

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        cap.statements.append(statement)

    sa_event.listen(target, "before_cursor_execute", _on_execute)
    try:
        yield cap
    finally:
        sa_event.remove(target, "before_cursor_execute", _on_execute)


@pytest_asyncio.fixture
async def public_library(session: AsyncSession) -> dict:
    """Two public books with themes, one private book — the private one is what
    proves the listing filters rather than just counting everything."""
    owner = User(telegram_id=TG_A, username="owner", full_name="Owner")
    session.add(owner)
    await session.flush()

    pub_a = Book(user_id=owner.id, title="Public A", original_pdf_path="a.pdf",
                 pdf_checksum="aa", is_public=True, uploaded_by_user_id=TG_A)
    pub_b = Book(user_id=owner.id, title="Public B", original_pdf_path="b.pdf",
                 pdf_checksum="bb", is_public=True, uploaded_by_user_id=TG_A)
    private = Book(user_id=owner.id, title="Private", original_pdf_path="p.pdf",
                   pdf_checksum="pp", is_public=False, uploaded_by_user_id=TG_A)
    session.add_all([pub_a, pub_b, private])
    await session.flush()

    # extracted_text is the column C-33 was worried about: give it real bulk so
    # that pulling it would be unmistakable.
    bulk = "x" * 50_000
    for book, n in ((pub_a, 3), (pub_b, 1), (private, 2)):
        for i in range(n):
            session.add(BookTheme(
                book_id=book.id, title=f"Theme {i}", order_index=i,
                page_start=i * 10 + 1, page_end=i * 10 + 9, extracted_text=bulk,
            ))
    await session.flush()
    return {"pub_a": pub_a, "pub_b": pub_b, "private": private}


async def test_listing_public_books_never_selects_extracted_text(
    session: AsyncSession, public_library
):
    """AUDIT C-33 claimed this listing drags multi-KB extracted_text for a
    <=300-book page. Measured, it does not, and this pins that.

    The count query selects book_id and count(*) only, and Book.themes is
    lazy="select" with nothing in the listing touching it — so Postgres never
    detoasts the column. What this guards against is a future change that
    WOULD introduce the defect: a selectinload(Book.themes), or flipping the
    relationship to lazy="selectin", either of which silently turns a 96 kB
    heap read into megabytes of TOAST.
    """
    with _capture_sql(session) as cap:
        await moderation.list_public_books(session)

    assert cap.selects, "captured no SQL at all — this test would pass vacuously"
    offending = [s for s in cap.selects if "extracted_text" in s.lower()]
    assert not offending, (
        "the moderation listing now pulls extracted_text:\n"
        + "\n".join(s[:200] for s in offending)
    )


async def test_listing_public_books_does_not_lazy_load_themes_per_book(
    session: AsyncSession, public_library
):
    """The other way this page could degrade: one themes query per book. The
    theme counts must come from a single aggregate, not N round-trips."""
    with _capture_sql(session) as cap:
        await moderation.list_public_books(session)

    theme_queries = [s for s in cap.selects if "book_themes" in s.lower()]
    assert len(theme_queries) == 1, (
        f"expected one aggregate over book_themes, got {len(theme_queries)} "
        f"(an N+1 lazy load?)"
    )


async def test_list_public_books_counts_themes_and_hides_private_books(
    session: AsyncSession, public_library
):
    """Plain correctness, which had no coverage at all: public only, with each
    book's live theme count."""
    rows = await moderation.list_public_books(session)
    by_title = {r["title"]: r for r in rows}

    assert "Private" not in by_title, "a non-public book appeared in the library"
    assert by_title["Public A"]["themes"] == 3
    assert by_title["Public B"]["themes"] == 1


# ── the ?days= window validator ──────────────────────────────────────────────

def test_validated_days_accepts_the_default_and_both_boundaries():
    from app.admin.web.context import DEFAULT_DAYS, MAX_DAYS, MIN_DAYS, validated_days

    assert validated_days() == DEFAULT_DAYS
    assert validated_days(MIN_DAYS) == MIN_DAYS
    assert validated_days(MAX_DAYS) == MAX_DAYS


def test_validated_days_rejects_rather_than_clamping():
    """Clamping is the dangerous option. `?days=-5` quietly answered as a
    30-day window produces a spend figure the admin cannot distinguish from a
    real one — and reconciling against Google's bill is exactly where that
    silence costs money."""
    from app.admin.web.context import BadRequest, MAX_DAYS, MIN_DAYS, validated_days

    for bad in (0, -1, -365, MAX_DAYS + 1, 99_999):
        with pytest.raises(BadRequest):
            validated_days(bad)
    assert MIN_DAYS == 1 and MAX_DAYS == 365


def test_validated_days_error_names_the_bounds_and_the_value():
    """The 400 body is the only feedback an admin gets from a hand-typed URL."""
    from app.admin.web.context import BadRequest, validated_days

    with pytest.raises(BadRequest) as exc:
        validated_days(99_999)
    assert "99999" in exc.value.message
    assert "365" in exc.value.message


async def test_book_usage_skips_teachers_with_no_book_footprint(session: AsyncSession):
    """The page is a book-limit view. A teacher who has never touched a book is
    noise on it."""
    session.add_all([
        User(telegram_id=TG_A, username="bookless", full_name="No Books"),
        User(telegram_id=TG_B, username="limited", full_name="Has A Limit", book_limit=3),
    ])
    await session.flush()

    rows = await book_usage.book_usage_rows(session)
    ids = [r["telegram_id"] for r in rows]
    assert TG_B in ids
    assert TG_A not in ids


# ── payments ledger: revenue, profit, and the two blocked segments ───────────
#
# Both sides of "profit" are checked against HAND-COMPUTED numbers, for the
# same reason the cost arithmetic above is: a wrong FX rate or a lifetime/30-day
# mix-up does not crash, it renders a plausible wrong number about money.

from app.models.payment import Payment  # noqa: E402
from app.services.plans import (PLANS, PRO, STANDART, is_trial, plan_for,  # noqa: E402
                                plan_name)

ADMIN_TG = 200_999


async def _user(session, tg: int, **kw) -> User:
    u = User(telegram_id=tg, username=f"u{tg}", full_name=f"User {tg}", **kw)
    session.add(u)
    await session.flush()
    return u


def test_payment_plan_keys_are_exactly_the_plans_that_exist():
    """plan_key is a String(16), not an enum, so nothing in the database stops
    a typo. plans.PLANS is the source of truth; this is the guard that makes
    the loose column safe, and it is why the migration did not reach for an
    ALTER TYPE."""
    assert set(PLANS) == {"standart", "pro"}, (
        "a tier was added or renamed — check record_payment's validation and "
        "the revenue page's by-tariff grouping still cover it")
    for key, plan in PLANS.items():
        assert len(key) <= 16, f"{key!r} will not fit payments.plan_key"


def test_the_prices_this_phase_was_built_on_have_not_moved():
    """Pricing moved once already (50k->75k, 100k->150k, commit 3572928) and
    migration 029 exists because of it. If it moves again, amount_som on
    historical rows is still right — that is the point of storing what was
    collected — but this test says so out loud instead of letting the change
    pass unremarked."""
    assert STANDART.price_som == 75_000 and STANDART.test_limit == 20
    assert STANDART.check_limit == 400 and STANDART.book_limit == 4
    assert PRO.price_som == 150_000 and PRO.test_limit == 40
    assert PRO.check_limit == 800 and PRO.book_limit == 8


async def test_record_payment_writes_the_ledger_and_applies_the_plan(session: AsyncSession):
    """The panel's sale action: one call, two effects. The plan half is
    delegated to admin_users.set_plan — the SAME function the bot's /plan uses
    — so the limits it sets are the plan's real bundle, not a second copy."""
    await _user(session, TG_A)
    p = await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 150_000)

    assert p.plan_key == "pro" and p.amount_som == 150_000
    assert p.method == "manual" and p.recorded_by == ADMIN_TG
    assert p.period_days == 30

    user = (await session.execute(
        select(User).where(User.telegram_id == TG_A))).scalar_one()
    assert user.monthly_variant_limit == PRO.test_limit
    assert user.monthly_check_limit == PRO.check_limit
    assert user.book_limit == PRO.book_limit
    assert user.price_som_at_purchase == PRO.price_som
    assert plan_name(user) == "Pro"


async def test_recording_a_payment_without_applying_records_history_only(session: AsyncSession):
    """Back-filling a transfer from two months ago must NOT hand out a fresh
    30-day window today."""
    u = await _user(session, TG_A)
    before = u.monthly_variant_limit
    await revenue.record_payment(session, ADMIN_TG, TG_A, "standart", 75_000,
                                 apply_plan=False)
    user = (await session.execute(
        select(User).where(User.telegram_id == TG_A))).scalar_one()
    assert user.monthly_variant_limit == before, "apply_plan=False granted a plan"
    prof = await revenue.user_profit(session, user)
    assert prof["revenue_som"] == 75_000, "the money was not recorded either"


async def test_record_payment_rejects_bad_input_before_touching_the_database(session: AsyncSession):
    await _user(session, TG_A)
    for bad_plan in ("premium", "", "PRO x"):
        with pytest.raises(ValueError):
            await revenue.record_payment(session, ADMIN_TG, TG_A, bad_plan, 75_000)
    for bad_amount in (0, -1):
        with pytest.raises(ValueError):
            await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", bad_amount)
    assert (await session.execute(select(func.count()).select_from(Payment))).scalar_one() == 0


async def test_amount_collected_is_stored_not_derived_from_todays_list_price(session: AsyncSession):
    """THE reason amount_som is a column. A discounted or grandfathered sale
    must report what came in, not what the tier costs today — the same
    reasoning that put users.price_som_at_purchase in migration 029, applied
    per event."""
    u = await _user(session, TG_A)
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 100_000,
                                 note="eski narx", apply_plan=False)
    rows = await revenue.user_payments(session, u.id)
    assert rows[0]["amount_som"] == 100_000 != PRO.price_som
    ov = await revenue.overview(session)
    pro = next(r for r in ov["by_plan"] if r["key"] == "pro")
    assert pro["revenue_som"] == 100_000
    assert pro["list_price_som"] == 150_000, "the list price is still shown for comparison"


async def test_profit_is_revenue_minus_that_users_gemini_cost(session: AsyncSession, fake_prices):
    """Hand-computed end to end: 1M DEAR prompt tokens @ $100/M = $100.00,
    times the configured FX, subtracted from what they paid."""
    u = await _user(session, TG_A)
    session.add(_usage(user_id=TG_A, model=DEAR, prompt_tokens=1_000_000))
    await session.flush()
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 150_000, apply_plan=False)

    prof = await revenue.user_profit(session, u)
    assert prof["revenue_som"] == 150_000
    assert prof["cost_usd"] == pytest.approx(100.0)
    assert prof["cost_som"] == pytest.approx(100.0 * settings.UZS_PER_USD)
    assert prof["profit_som"] == pytest.approx(150_000 - 100.0 * settings.UZS_PER_USD)


async def test_profit_counts_every_payment_and_ignores_other_users_cost(
    session: AsyncSession, fake_prices
):
    u = await _user(session, TG_A)
    await _user(session, TG_B)
    session.add_all([
        _usage(user_id=TG_A, model=CHEAP, prompt_tokens=1_000_000),   # $1
        _usage(user_id=TG_B, model=DEAR, prompt_tokens=1_000_000),    # $100, not ours
        _usage(user_id=None, model=DEAR, prompt_tokens=1_000_000),    # unattributed
    ])
    await session.flush()
    await revenue.record_payment(session, ADMIN_TG, TG_A, "standart", 75_000, apply_plan=False)
    await revenue.record_payment(session, ADMIN_TG, TG_A, "standart", 75_000, apply_plan=False)

    prof = await revenue.user_profit(session, u)
    assert prof["payments"] == 2 and prof["revenue_som"] == 150_000
    assert prof["cost_usd"] == pytest.approx(1.0), "another user's or an anonymous cost leaked in"


async def test_profit_is_lifetime_on_both_sides_not_a_30_day_window(
    session: AsyncSession, fake_prices
):
    """A lifetime revenue against a 30-day cost would make every long-standing
    customer look more profitable the longer they stay. Both sides must reach
    back equally far."""
    u = await _user(session, TG_A)
    old = datetime.now(timezone.utc) - timedelta(days=200)
    session.add(_usage(user_id=TG_A, model=CHEAP, prompt_tokens=1_000_000, created_at=old))
    await session.flush()
    await revenue.record_payment(session, ADMIN_TG, TG_A, "standart", 75_000,
                                 paid_at=old, apply_plan=False)
    prof = await revenue.user_profit(session, u)
    assert prof["revenue_som"] == 75_000, "a 200-day-old payment was dropped"
    assert prof["cost_usd"] == pytest.approx(1.0), "a 200-day-old cost row was dropped"


async def test_margin_is_none_rather_than_zero_when_nothing_was_collected(session: AsyncSession):
    u = await _user(session, TG_A)
    prof = await revenue.user_profit(session, u)
    assert prof["revenue_som"] == 0
    assert prof["margin_pct"] is None, "a free user was reported as 0% margin, not undefined"
    assert prof["underwater"] is False, "a user who never paid is not underwater"


async def test_overview_totals_by_tariff_and_flags_users_costing_more_than_they_pay(
    session: AsyncSession, fake_prices
):
    await _user(session, TG_A)
    await _user(session, TG_B)
    # A: pays 150k, costs $1 (=12,000 som) -> profitable
    # B: pays 75k, costs $100 (=1,200,000 som) -> deeply underwater
    session.add_all([
        _usage(user_id=TG_A, model=CHEAP, prompt_tokens=1_000_000),
        _usage(user_id=TG_B, model=DEAR, prompt_tokens=1_000_000),
    ])
    await session.flush()
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 150_000, apply_plan=False)
    await revenue.record_payment(session, ADMIN_TG, TG_B, "standart", 75_000, apply_plan=False)

    ov = await revenue.overview(session)
    assert ov["total_revenue_som"] == 225_000
    by = {r["key"]: r for r in ov["by_plan"]}
    assert by["pro"]["revenue_som"] == 150_000 and by["pro"]["payments"] == 1
    assert by["standart"]["revenue_som"] == 75_000

    flagged = {r["telegram_id"] for r in ov["underwater"]}
    assert TG_B in flagged, "the user costing 16x what they paid was not flagged"
    assert TG_A not in flagged, "a profitable user was flagged"


async def test_overview_flags_a_free_user_who_costs_money(session: AsyncSession, fake_prices):
    """Revenue 0 with any cost at all is the clearest case of cost exceeding
    revenue. Excluding free users would hide the segment most worth watching."""
    await _user(session, TG_A)
    session.add(_usage(user_id=TG_A, model=CHEAP, prompt_tokens=1_000_000))
    await session.flush()
    ov = await revenue.overview(session)
    assert TG_A in {r["telegram_id"] for r in ov["underwater"]}


async def test_overview_reports_unattributed_cost_separately(session: AsyncSession, fake_prices):
    """Most gemini_usage rows carry no user_id. The page-level margin must
    count that cost, and must be able to say how much of it there is —
    otherwise per-user profit silently reads as the whole picture."""
    await _user(session, TG_A)
    session.add_all([
        _usage(user_id=TG_A, model=CHEAP, prompt_tokens=1_000_000),  # $1 attributed
        _usage(user_id=None, model=DEAR, prompt_tokens=1_000_000),   # $100 not
    ])
    await session.flush()
    ov = await revenue.overview(session)
    assert ov["cost"]["attributed_usd"] == pytest.approx(1.0)
    assert ov["cost"]["unattributed_usd"] == pytest.approx(100.0)
    assert ov["cost"]["total_usd"] == pytest.approx(101.0)
    assert ov["cost"]["attributed_pct"] == pytest.approx(50.0)
    # The headline margin uses the FULL cost, not just the attributed slice.
    assert ov["total_cost_som"] == pytest.approx(101.0 * settings.UZS_PER_USD)


async def test_overview_shows_every_tier_even_one_that_never_sold(session: AsyncSession):
    ov = await revenue.overview(session)
    assert {r["key"] for r in ov["by_plan"]} == set(PLANS)
    assert all(r["revenue_som"] == 0 and r["avg_som"] is None for r in ov["by_plan"])
    assert ov["margin_pct"] is None


# ── the two segments the Messaging phase was blocked on ──────────────────────

async def test_segment_c_used_a_paid_plan_before_survives_the_plan_being_revoked(
    session: AsyncSession
):
    """THE reason this segment needed the ledger. A teacher who paid in June and
    was revoked in July has NO trace of it in users — set_plan and revoke both
    overwrite the limit columns in place. Only the payment event remembers."""
    paid = await _user(session, TG_A)
    await _user(session, TG_B)  # never paid
    await revenue.record_payment(session, ADMIN_TG, TG_A, "standart", 75_000)

    # ...and now they lapse completely: access revoked, limits cleared.
    from app.services import admin_users as au
    await au.set_blocked(session, ADMIN_TG, TG_A, True)
    lapsed_user = (await session.execute(
        select(User).where(User.telegram_id == TG_A))).scalar_one()
    lapsed_user.monthly_variant_limit = None
    lapsed_user.monthly_check_limit = None
    await session.flush()

    ever = await revenue.ever_paid_user_ids(session)
    assert paid.id in ever, "a past customer vanished the moment their plan did"
    assert plan_name(lapsed_user) == "Bepul", "the fixture did not actually lapse them"


async def test_segment_d_late_this_month_is_paid_before_and_coverage_expired(
    session: AsyncSession
):
    current = await _user(session, TG_A)       # paid 5 days ago — still covered
    late = await _user(session, TG_B)          # paid 45 days ago — lapsed
    never = await _user(session, 200_003)      # never paid — in NEITHER segment
    now = datetime.now(timezone.utc)
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 150_000,
                                 paid_at=now - timedelta(days=5), apply_plan=False)
    await revenue.record_payment(session, ADMIN_TG, TG_B, "standart", 75_000,
                                 paid_at=now - timedelta(days=45), apply_plan=False)

    lapsed = await revenue.lapsed_payer_ids(session)
    assert late.id in lapsed
    assert current.id not in lapsed, "a teacher 5 days into a 30-day period is not late"
    assert never.id not in lapsed, "someone who never paid is not a LATE payer"
    # (d) is a strict subset of (c) — a late payer is by definition a past one.
    assert lapsed <= await revenue.ever_paid_user_ids(session)


async def test_a_renewal_takes_a_user_back_out_of_the_late_segment(session: AsyncSession):
    u = await _user(session, TG_A)
    now = datetime.now(timezone.utc)
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 150_000,
                                 paid_at=now - timedelta(days=45), apply_plan=False)
    assert u.id in await revenue.lapsed_payer_ids(session)
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 150_000, apply_plan=False)
    assert u.id not in await revenue.lapsed_payer_ids(session), (
        "lapsed_payer_ids read an old payment instead of the LATEST one")


async def test_late_uses_the_period_stored_on_the_row_not_todays_plan_days(session: AsyncSession):
    """A 60-day prepayment must not be scored late after 30. period_days lives
    on the row precisely so a change to plans.PLAN_DAYS cannot retroactively
    re-judge history."""
    u = await _user(session, TG_A)
    now = datetime.now(timezone.utc)
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 300_000,
                                 paid_at=now - timedelta(days=45),
                                 period_days=60, apply_plan=False)
    assert u.id not in await revenue.lapsed_payer_ids(session)


async def test_access_until_does_not_decide_the_late_segment(session: AsyncSession):
    """A /grant or /extend moves access_until with no money arriving. Has
    access and has paid for this period are different questions, and the
    segment is about the second one."""
    u = await _user(session, TG_A)
    now = datetime.now(timezone.utc)
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 150_000,
                                 paid_at=now - timedelta(days=45), apply_plan=False)
    from app.services import admin_users as au
    await au.grant(session, ADMIN_TG, TG_A, days=90, uses=None, note="comp")

    refreshed = (await session.execute(
        select(User).where(User.telegram_id == TG_A))).scalar_one()
    assert refreshed.access_until > now, "the fixture did not actually extend access"
    assert u.id in await revenue.lapsed_payer_ids(session), (
        "a comped extension was mistaken for a payment")


async def test_recording_a_payment_is_audited(session: AsyncSession):
    from app.models.admin_log import ADMIN_LOG_ACTIONS, AdminLog
    await _user(session, TG_A)
    await revenue.record_payment(session, ADMIN_TG, TG_A, "pro", 150_000, apply_plan=False)
    row = (await session.execute(
        select(AdminLog).where(AdminLog.action == "payment"))).scalars().one()
    assert row.target == TG_A and row.admin_id == ADMIN_TG
    assert row.params["amount_som"] == 150_000 and row.params["plan"] == "pro"
    assert "payment" in ADMIN_LOG_ACTIONS, "the audit filter cannot select this action"


def test_payment_model_and_migration_034_do_not_drift():
    """The ORM metadata is what create_all() builds in tests; the migration is
    what production gets. When they disagree the tests pass against a schema
    production does not have — which is why migration 031 has a test just like
    this one.

    Checked here: both indexes the migration creates, and the CASCADE on the
    user FK (a financial row must not be able to outlive its user as an orphan).
    """
    import re
    from pathlib import Path

    from app.models.payment import Payment

    names = {ix.name for ix in Payment.__table__.indexes}
    assert names == {"ix_payments_user_id_paid_at", "ix_payments_paid_at"}, names

    fks = list(Payment.__table__.c.user_id.foreign_keys)
    assert len(fks) == 1 and fks[0].ondelete == "CASCADE"

    migration = (Path(__file__).resolve().parents[1]
                 / "migrations" / "versions" / "034_payments.py").read_text(encoding="utf-8")
    for name in names:
        assert f'"{name}"' in migration, f"{name} is on the model but not in migration 034"
    assert 'ondelete="CASCADE"' in migration
    assert re.search(r'down_revision\s*=\s*"033"', migration), (
        "034 no longer chains off 033 — check nothing else claimed that slot")


def test_the_payments_ledger_did_not_revive_the_dead_subscriptions_table():
    """subscriptions is mapped, migrated and written by NOTHING. The revenue
    phase deliberately left it that way rather than making it a second source
    of truth for a user's plan. If something starts writing it, the decision
    recorded in app/models/payment.py needs revisiting first."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    writers = []
    for src in (repo_root / "app").rglob("*.py"):
        text = src.read_text(encoding="utf-8", errors="ignore")
        if "Subscription(" in text and src.name != "subscription.py":
            writers.append(str(src.relative_to(repo_root)))
    assert not writers, (
        "something now constructs a Subscription: " + ", ".join(writers)
        + " — resolve which of it and `users` is authoritative for a plan first")


# ── messaging: segments, and that delivery is NOT reimplemented ──────────────
#
# The segment tests are the point of this block. The dashboard's P1 bug was a
# segment re-derived in SQL with a condition that was no part of its one
# definition, and the two answers disagreed silently. Every segment here is
# therefore tested against the DEFINING function's own edge cases — the admin
# with a live trial meter, the legacy row with neither limit nor meter, the
# lapsed payer — not just against a happy-path row.

from app.admin.web.services import messaging  # noqa: E402
from app.services import broadcast  # noqa: E402


class _StubBot:
    """Records sends. NOTHING in this suite may reach Telegram."""

    def __init__(self, fail_for=(), flood_for=()):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = set(fail_for)
        self.flood_for = set(flood_for)
        self._flooded: set[int] = set()

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.flood_for and chat_id not in self._flooded:
            from aiogram.exceptions import TelegramRetryAfter
            self._flooded.add(chat_id)
            raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
        if chat_id in self.fail_for:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="bot was blocked")
        self.sent.append((chat_id, text))
        return True


async def _seed_segment_users(session):
    """One row of every shape the five segments branch on."""
    now = datetime.now(timezone.utc)
    rows = {
        # trial tier, meter spent -> (a)
        "trial_used": User(telegram_id=300_001, username="used", full_name="Used Trial",
                           monthly_variant_limit=None, uses_left=0),
        # trial tier, meter untouched -> (b)
        "trial_fresh": User(telegram_id=300_002, username="fresh", full_name="Fresh Trial",
                            monthly_variant_limit=None,
                            uses_left=settings.TRIAL_USES),
        # paid, live access -> (e)
        "paid_active": User(telegram_id=300_003, username="active", full_name="Active Paid",
                            monthly_variant_limit=20, monthly_check_limit=400,
                            book_limit=4, access_until=now + timedelta(days=10)),
        # paid tier but expired -> NOT (e)
        "paid_expired": User(telegram_id=300_004, username="expired", full_name="Expired Paid",
                             monthly_variant_limit=20, monthly_check_limit=400,
                             access_until=now - timedelta(days=2)),
        # paid tier but blocked -> NOT (e)
        "paid_blocked": User(telegram_id=300_005, username="blocked", full_name="Blocked Paid",
                             monthly_variant_limit=20, monthly_check_limit=400,
                             is_blocked=True, access_until=now + timedelta(days=10)),
        # Admins with a live meter AND with a spent one. is_trial() excludes
        # admins outright, so NEITHER trial segment may contain either row.
        #
        # BOTH are needed, and the spent one is the load-bearing case: with only
        # the untouched-meter admin, a _trial_used() that dropped is_trial() and
        # re-derived the tier from monthly_variant_limit still passed, because
        # an untouched meter fails the "spent" half anyway. Found by mutating
        # is_trial() out of _trial_used and watching the suite stay green.
        "admin": User(telegram_id=300_006, username="adm", full_name="An Admin",
                      is_admin=True, uses_left=settings.TRIAL_USES),
        "admin_spent": User(telegram_id=300_008, username="adm2",
                            full_name="Admin Who Generated",
                            is_admin=True, monthly_variant_limit=None, uses_left=0),
        # legacy: no paid pool AND no meter -> is_trial() excludes it too.
        "legacy": User(telegram_id=300_007, username="legacy", full_name="Legacy Row",
                       monthly_variant_limit=None, uses_left=None),
    }
    session.add_all(list(rows.values()))
    await session.flush()
    return rows


def _tg(users):
    return {u.telegram_id for u in users}


async def test_segment_a_used_trial_is_the_trial_tier_with_a_spent_meter(session: AsyncSession):
    rows = await _seed_segment_users(session)
    members = await messaging.segment_members(session, "trial_used")
    assert _tg(members) == {300_001}, (
        "used-trial must be exactly the trial-tier users whose meter was spent")


async def test_segment_b_never_used_trial_is_the_trial_tier_with_an_untouched_meter(
    session: AsyncSession
):
    rows = await _seed_segment_users(session)
    members = await messaging.segment_members(session, "trial_unused")
    assert _tg(members) == {300_002}


async def test_the_two_trial_segments_partition_is_trial_exactly(session: AsyncSession):
    """THE guard against re-deriving the tier. (a) and (b) must be disjoint and
    together equal plans.is_trial() — so an admin or a legacy row cannot leak
    into either, and a trial user cannot fall through both."""
    await _seed_segment_users(session)
    used = _tg(await messaging.segment_members(session, "trial_used"))
    unused = _tg(await messaging.segment_members(session, "trial_unused"))
    everyone = (await session.execute(select(User))).scalars().all()
    by_definition = {u.telegram_id for u in everyone if is_trial(u)}

    assert not (used & unused), "a user is in BOTH trial segments"
    assert used | unused == by_definition, (
        "the trial segments do not partition plans.is_trial() — "
        f"segments={sorted(used | unused)} is_trial={sorted(by_definition)}")


async def test_an_admin_with_a_live_trial_meter_is_in_neither_trial_segment(
    session: AsyncSession
):
    """is_trial() excludes admins on purpose (a quota gate has no is_admin
    bypass of its own). A segment that re-derived the condition would have
    included this row and messaged the admin their own announcement."""
    await _seed_segment_users(session)
    for key in ("trial_used", "trial_unused"):
        members = _tg(await messaging.segment_members(session, key))
        assert 300_006 not in members, f"{key} included an admin (untouched meter)"
        assert 300_008 not in members, f"{key} included an admin (spent meter)"


async def test_a_legacy_row_with_no_limit_and_no_meter_is_in_neither_trial_segment(
    session: AsyncSession
):
    await _seed_segment_users(session)
    for key in ("trial_used", "trial_unused"):
        assert 300_007 not in _tg(await messaging.segment_members(session, key)), key


async def test_segment_e_active_paid_excludes_expired_and_blocked(session: AsyncSession):
    await _seed_segment_users(session)
    members = await messaging.segment_members(session, "paid_active")
    assert _tg(members) == {300_003}, (
        "active-paid must exclude the expired and the blocked paid users")


async def test_segment_e_agrees_with_plan_for_on_who_is_paid(session: AsyncSession):
    """plan_for() is the definition of 'on a paid tier'. Segment (e) narrows it
    by access and block state and must not disagree about the tier itself."""
    await _seed_segment_users(session)
    members = await messaging.segment_members(session, "paid_active")
    for u in members:
        assert plan_for(u) is not None, f"{u.telegram_id} is in active-paid but has no plan"


async def test_segments_c_and_d_come_from_the_payments_ledger(session: AsyncSession):
    """(c) and (d) are the two the Messaging phase was blocked on. They must be
    the ledger's answer, not a re-derivation from the user's current limits."""
    rows = await _seed_segment_users(session)
    now = datetime.now(timezone.utc)
    # The expired paid user paid 50 days ago and never renewed -> (c) and (d).
    await revenue.record_payment(session, 999, 300_004, "standart", 75_000,
                                 paid_at=now - timedelta(days=50), apply_plan=False)
    # The active paid user paid 3 days ago -> (c) only.
    await revenue.record_payment(session, 999, 300_003, "pro", 150_000,
                                 paid_at=now - timedelta(days=3), apply_plan=False)

    before = _tg(await messaging.segment_members(session, "paid_before"))
    late = _tg(await messaging.segment_members(session, "paid_late"))
    assert before == {300_003, 300_004}
    assert late == {300_004}
    assert late <= before, "a late payer must also be a past payer"


async def test_an_unknown_segment_key_raises_rather_than_messaging_everyone(
    session: AsyncSession
):
    """A typo must not silently fall through to 'all users'."""
    with pytest.raises(ValueError):
        await messaging.segment_members(session, "everyone")


async def test_segment_counts_match_segment_members_one_for_one(session: AsyncSession):
    """The picker shows counts from a different code path than the send uses.
    If they drift, the operator confirms '12 users' and messages a different
    number."""
    await _seed_segment_users(session)
    counts = {c["key"]: c["count"] for c in await messaging.segment_counts(session)}
    assert set(counts) == set(messaging.SEGMENT_KEYS)
    for key in messaging.SEGMENT_KEYS:
        members = await messaging.segment_members(session, key)
        assert counts[key] == len(members), f"{key}: picker says {counts[key]}, send has {len(members)}"


# ── delivery goes through broadcast.py, and nowhere else ─────────────────────

async def test_sending_to_a_segment_uses_the_shared_broadcast_path(session: AsyncSession):
    """Reuse, not a parallel send path: the recipients must arrive with
    broadcast.PREFIX, which only run_broadcast applies."""
    await _seed_segment_users(session)
    bot = _StubBot()
    result = await messaging.send_to_segment(session, bot, 999, "paid_active", "Salom")
    assert result == {"recipients": 1, "sent": 1, "failed": 0, "segment": "paid_active"}
    assert [c for c, _ in bot.sent] == [300_003]
    assert bot.sent[0][1].startswith(broadcast.PREFIX), (
        "the message did not go through broadcast.run_broadcast")
    assert "Salom" in bot.sent[0][1]


async def test_a_single_message_uses_the_same_path_as_the_bots_message_command(
    session: AsyncSession
):
    """A teacher must not be able to tell whether the admin used the panel or
    the Telegram /message command: same DM_PREFIX, same helper."""
    await _seed_segment_users(session)
    bot = _StubBot()
    ok = await messaging.send_to_one(session, bot, 999, 300_001, "Shaxsiy xabar")
    assert ok is True
    assert bot.sent[0][0] == 300_001
    assert bot.sent[0][1].startswith(broadcast.DM_PREFIX)
    assert broadcast.PREFIX not in bot.sent[0][1], "a DM was sent with the broadcast prefix"


async def test_a_user_who_blocked_the_bot_is_counted_failed_not_crashed(session: AsyncSession):
    """One dead recipient must never abort the rest of a broadcast."""
    await _seed_segment_users(session)
    bot = _StubBot(fail_for={300_001})
    # Put two users in the same segment by spending the fresh one's meter.
    fresh = (await session.execute(
        select(User).where(User.telegram_id == 300_002))).scalar_one()
    fresh.uses_left = 0
    await session.flush()

    result = await messaging.send_to_segment(session, bot, 999, "trial_used", "Salom")
    assert result["recipients"] == 2
    assert result["sent"] == 1 and result["failed"] == 1
    assert [c for c, _ in bot.sent] == [300_002], "the live recipient was skipped too"


async def test_flood_control_waits_and_retries_rather_than_dropping(session: AsyncSession):
    """TelegramRetryAfter must cost the recipient a delay, not the message."""
    await _seed_segment_users(session)
    bot = _StubBot(flood_for={300_003})
    result = await messaging.send_to_segment(session, bot, 999, "paid_active", "Salom")
    assert result["sent"] == 1 and result["failed"] == 0
    assert [c for c, _ in bot.sent] == [300_003]


async def test_an_empty_segment_sends_nothing_and_says_so(session: AsyncSession):
    """No users must not mean 'no filter' — the classic broadcast disaster."""
    bot = _StubBot()
    result = await messaging.send_to_segment(session, bot, 999, "paid_before", "Salom")
    assert result == {"recipients": 0, "sent": 0, "failed": 0, "segment": "paid_before"}
    assert bot.sent == []


async def test_a_segment_broadcast_is_audited_with_its_segment_as_the_scope(
    session: AsyncSession
):
    from app.models.admin_log import AdminLog
    await _seed_segment_users(session)
    await messaging.send_to_segment(session, _StubBot(), 999, "paid_active", "Salom")
    row = (await session.execute(
        select(AdminLog).where(AdminLog.action == "broadcast"))).scalars().one()
    assert row.params["scope"] == "paid_active", "the audit row does not say which group"
    assert row.params["sent"] == 1


# ── rate limiting: verification, not a rebuild ───────────────────────────────

def test_the_send_spacing_stays_under_telegrams_bulk_ceiling():
    """Telegram tolerates ~30 messages/second to distinct users. The spacing is
    the only thing standing between a broadcast and a flood ban, so it is
    pinned rather than left to drift.

    Measured with a stub bot at this spacing: 17 messages in the busiest
    one-second window with a zero-latency API (the worst case — real network
    latency only lowers it), against the ~30/s ceiling.
    """
    assert broadcast.DEFAULT_SLEEP_BETWEEN >= 1 / 30, (
        f"spacing {broadcast.DEFAULT_SLEEP_BETWEEN}s exceeds ~30 msg/s — flood-ban risk")
    nominal = 1 / broadcast.DEFAULT_SLEEP_BETWEEN
    assert nominal <= 25, f"nominal rate {nominal:.0f}/s leaves no headroom under 30/s"


async def test_every_recipient_is_spaced_not_fired_in_parallel(session: AsyncSession):
    """asyncio.gather over recipients would be far faster and would get the bot
    flood-banned. Prove the sends are sequential and spaced by measuring the
    wall-clock of a run whose stub returns instantly."""
    import time

    bot = _StubBot()
    ids = list(range(400_001, 400_011))  # 10 recipients -> 9 gaps
    t0 = time.perf_counter()
    sent, failed = await broadcast.run_broadcast(bot, ids, "x")
    elapsed = time.perf_counter() - t0

    assert (sent, failed) == (10, 0)
    floor = 9 * broadcast.DEFAULT_SLEEP_BETWEEN
    assert elapsed >= floor * 0.8, (
        f"10 sends took {elapsed:.3f}s; spacing alone should cost >= {floor:.3f}s — "
        "the per-recipient delay is gone")


def test_the_slow_broadcast_threshold_matches_a_sane_request_time():
    """The UI warns above this size. If someone raises it, the warning stops
    firing before the request stops being survivable."""
    seconds = messaging.estimate_seconds(messaging.SLOW_BROADCAST_THRESHOLD)
    assert seconds <= 30, (
        f"a {messaging.SLOW_BROADCAST_THRESHOLD}-recipient send costs {seconds:.0f}s of "
        "spacing alone — past a reasonable HTTP request, so the warning is too late")


# ── the descoped things must stay descoped ───────────────────────────────────

def test_messaging_added_no_storage_of_its_own():
    """No message history log, no templates, no delivery-status tracking — all
    explicitly out of scope. The only record of a send is the admin_log row
    broadcast.py already wrote before this phase existed."""
    import app.models as models

    tables = set(models.Base.metadata.tables) if hasattr(models, "Base") else None
    if tables is None:
        from app.database import Base
        tables = set(Base.metadata.tables)
    forbidden = {t for t in tables
                 if "message" in t or "template" in t or "delivery" in t}
    assert not forbidden, f"messaging created storage it was told not to: {forbidden}"


def test_messaging_service_reimplements_no_sending():
    """Every send must be a call into broadcast.py. A bare bot.send_message in
    this module would be a second send path with its own prefix, its own error
    handling and its own (missing) spacing."""
    from pathlib import Path

    src = (Path(messaging.__file__)).read_text(encoding="utf-8")
    assert "bot.send_message" not in src, (
        "messaging.py calls the Telegram API directly instead of going through "
        "broadcast.py")
    assert "run_broadcast" in src and "send_direct" in src
