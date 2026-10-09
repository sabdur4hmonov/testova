"""
Final pricing tiers — the COMBINED book+variant test pool.

Three things are proven here, because three separate mechanisms carry the
tiers and each can fail independently:

  1. The pool is genuinely SHARED. Book generation and variant generation both
     consume `quota.TEST`, which maps to the SAME columns the variant meter
     always used — so 20 (Standart) / 40 (Pro) is one number across both paths,
     not two budgets that happen to be equal.
  2. Bepul's allowances are LIFETIME, not rolling. This is the part that does
     NOT fall out of the existing quota machinery: `_apply_rolling_reset`
     zeroes every period counter off one shared `period_start`, so a naive
     "limit = 1" would silently refill every 30 days. Generation rides
     `uses_left` (already lifetime, never reset by anything) and checking rides
     the new `check_count_lifetime` column.
  3. Shadow mode logs instead of blocking — for the PAID caps only. Bepul is
     enforced from day one: it is trial access against zero revenue, so a
     shadowed free tier is just an unmetered free tier.

Real Postgres for the enforcement paths: the per-run test database from the
conftest `engine` fixture, never the live one.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import settings
from app.services import plans

from tests.synthetic_ids import TEST_ADMIN_ID


async def _mk(sm, **kw):
    from app.models.user import User
    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        s.add(User(telegram_id=tg, username=f"c{tg}", full_name="Combined", **kw))
        await s.commit()
    return tg


async def _read(sm, tg):
    from sqlalchemy import select
    from app.models.user import User
    async with sm() as s:
        return (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one()


async def _drop(sm, tg):
    from sqlalchemy import delete
    from app.models.admin_log import AdminLog
    from app.models.user import User
    async with sm() as s:
        await s.execute(delete(AdminLog).where(AdminLog.target == tg))
        await s.execute(delete(User).where(User.telegram_id == tg))
        await s.commit()


# ── 1. The plan bundle ───────────────────────────────────────────────────────

def test_final_tier_numbers():
    assert plans.STANDART.price_som == 75_000
    assert plans.STANDART.test_limit == 20
    assert plans.STANDART.check_limit == 400
    assert plans.STANDART.book_limit == 4

    assert plans.PRO.price_som == 150_000
    assert plans.PRO.test_limit == 40
    assert plans.PRO.check_limit == 800
    assert plans.PRO.book_limit == 8


def test_tier_derivation_still_recognises_legacy_limits():
    """The thresholds moved 25→20 and 50→40, so every already-sold row must
    still derive its TIER — _tier_by is `>=`, so 25 >= 20 and 50 >= 40. A
    paying teacher losing their plan on deploy would be the worst possible
    regression here."""
    def tier(n):
        p = plans.plan_for(SimpleNamespace(monthly_variant_limit=n))
        return p.key if p else None

    assert tier(25) == "standart"      # already-sold Standart
    assert tier(50) == "pro"           # already-sold Pro
    assert tier(20) == "standart"      # the new bases
    assert tier(40) == "pro"
    assert tier(19) is None            # below Standart is Bepul
    assert tier(None) is None


def test_legacy_limits_render_as_a_top_up_not_a_new_tier():
    """A legacy row keeps its 25 until the next /plan renewal re-bundles it to
    20, so it really does hold 5 tests above the new base. The existing `+N`
    suffix says so rather than hiding it, and — the part that matters — the
    TIER NAME does not change and the price shown stays the tier's price."""
    legacy = SimpleNamespace(monthly_variant_limit=25, monthly_check_limit=500)
    assert plans.plan_name(legacy) == "Standart +5"
    assert plans.plan_for(legacy).price_som == 75_000

    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=20)) == "Standart"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=None)) == "Bepul"


def test_is_trial_predicate():
    """`is_trial` is the ONE definition of 'Bepul with a live trial meter'. It
    is deliberately narrower than 'not on a paid plan': an admin is never
    trial, and a fully-unconfigured legacy row (every limit NULL, no uses_left)
    stays unlimited rather than being retro-capped at one generation."""
    trial = SimpleNamespace(is_admin=False, monthly_variant_limit=None, uses_left=1)
    spent = SimpleNamespace(is_admin=False, monthly_variant_limit=None, uses_left=0)
    paid = SimpleNamespace(is_admin=False, monthly_variant_limit=20, uses_left=None)
    admin = SimpleNamespace(is_admin=True, monthly_variant_limit=None, uses_left=1)
    legacy = SimpleNamespace(is_admin=False, monthly_variant_limit=None, uses_left=None)

    assert plans.is_trial(trial) is True
    assert plans.is_trial(spent) is True      # exhausted is still the trial tier
    assert plans.is_trial(paid) is False
    assert plans.is_trial(admin) is False
    assert plans.is_trial(legacy) is False


# ── 2. The shared pool ───────────────────────────────────────────────────────

def test_test_kind_maps_to_the_variant_columns():
    """No migration for the pool: TEST reuses the columns the variant meter
    already had, so existing counters and admin setters keep working."""
    from app.services import quota
    assert quota._fields(quota.TEST) == (
        "monthly_variant_limit", "variant_count_this_period")
    # the old constant is an alias, so no call site or test silently changes meaning
    assert quota.VARIANT == quota.TEST
    assert quota._fields("variant") == quota._fields(quota.TEST)


async def test_book_and_variant_draw_from_one_pool(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm, monthly_variant_limit=2, uses_left=None)
    try:
        # Two generations of ANY mix exhaust the pool. The kind string is the
        # same for both paths, which is the whole point.
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.TEST) == (True, 1)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.TEST) == (True, 0)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.TEST) == (False, 0)
        row = await _read(sm, tg)
        assert row.variant_count_this_period == 2
        # the retired book-generation counter was never touched
        assert row.book_generation_count_this_period == 0
    finally:
        await _drop(sm, tg)


async def test_set_plan_assigns_the_whole_bundle_including_books(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm, uses_left=1)
    try:
        async with sm() as s:
            u = await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "standart")
        assert u.monthly_variant_limit == 20     # the combined pool
        assert u.monthly_check_limit == 400
        assert u.book_limit == 4                 # was assigned NOWHERE before
        assert u.uses_left is None               # trial meter neutralised
        # the retired meter stays NULL rather than being quietly revived
        assert u.monthly_book_generation_limit is None

        async with sm() as s:
            u = await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "pro")
        assert (u.monthly_variant_limit, u.monthly_check_limit, u.book_limit) == (40, 800, 8)
    finally:
        await _drop(sm, tg)


# ── 3. Bepul is LIFETIME, not rolling ────────────────────────────────────────

async def test_bepul_checks_do_not_refill_on_a_period_rollover(engine):
    """The regression this whole column exists to prevent. A trial user at
    their 2-check cap must STAY at it after 31 days — `_apply_rolling_reset`
    zeroes every period counter, so a rolling counter would hand out 2 more."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    tg = await _mk(sm, uses_left=1, monthly_variant_limit=None,
                   monthly_check_limit=2, period_start=now)
    try:
        for i in range(2):
            async with sm() as s:
                allowed, _ = await quota.try_consume(s, tg, quota.CHECK)
                assert allowed is True, f"check {i + 1} should be allowed"
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.CHECK) == (False, 0)

        # roll the window over by 31 days
        async with sm() as s:
            await s.execute(
                User.__table__.update().where(User.telegram_id == tg).values(
                    period_start=now - timedelta(days=31))
            )
            await s.commit()

        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.CHECK) == (False, 0), (
                "the trial's 2 checks refilled on a rollover — they are LIFETIME")
        row = await _read(sm, tg)
        assert row.check_count_lifetime == 2
        assert row.check_count_this_period == 0   # the rolling counter is unused here
    finally:
        await _drop(sm, tg)


async def test_paid_checks_still_reset_monthly(engine):
    """The other half of the same branch: a PAID user's checks are rolling, and
    that must not be collateral damage from the trial fix."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    tg = await _mk(sm, monthly_variant_limit=20, monthly_check_limit=1,
                   uses_left=None, period_start=now)
    try:
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.CHECK) == (True, 0)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.CHECK) == (False, 0)
        async with sm() as s:
            await s.execute(
                User.__table__.update().where(User.telegram_id == tg).values(
                    period_start=now - timedelta(days=31))
            )
            await s.commit()
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.CHECK) == (True, 0), (
                "a paid user's monthly checks must still refill")
        row = await _read(sm, tg)
        assert row.check_count_lifetime == 0      # the lifetime counter is unused here
    finally:
        await _drop(sm, tg)


async def test_bepul_remaining_reads_the_lifetime_counter():
    """`remaining` feeds the Hisobim screen. It must read the same counter the
    gate consumes, or the teacher sees '2 left' on an exhausted trial."""
    from app.services import quota

    trial = SimpleNamespace(
        is_admin=False, uses_left=1, monthly_variant_limit=None,
        monthly_check_limit=2, check_count_this_period=0,
        check_count_lifetime=2, period_start=None,
    )
    assert quota.remaining(trial, quota.CHECK) == 0


# ── 4. Shadow mode ───────────────────────────────────────────────────────────

class _LogSpy:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def _rec(self, event, **kw):
        self.events.append((event, kw))

    info = warning = error = _rec

    def names(self):
        return [e for e, _ in self.events]


async def test_shadow_mode_allows_and_logs_would_block(engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm, monthly_variant_limit=1, uses_left=None)
    spy = _LogSpy()
    monkeypatch.setattr(quota, "logger", spy)
    try:
        assert await quota.check_and_consume(sm, tg, quota.TEST, shadow=True) is True
        # at the cap now — shadow lets it through and says so
        assert await quota.check_and_consume(sm, tg, quota.TEST, shadow=True) is True
        assert "quota_would_block" in spy.names()

        # and it keeps COUNTING past the limit, which is the point: the cycle
        # exists to learn the real distribution, not just that a cap was hit.
        row = await _read(sm, tg)
        assert row.variant_count_this_period == 2

        # the peek must agree with the gate, or extraction is blocked before a
        # generation that shadow would then allow
        assert await quota.check_available(sm, tg, quota.TEST, shadow=True) is True
    finally:
        await _drop(sm, tg)


async def test_shadow_off_enforces_the_cap(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm, monthly_variant_limit=1, uses_left=None)
    try:
        assert await quota.check_and_consume(sm, tg, quota.TEST, shadow=False) is True
        assert await quota.check_and_consume(sm, tg, quota.TEST, shadow=False) is False
        assert await quota.check_available(sm, tg, quota.TEST, shadow=False) is False
    finally:
        await _drop(sm, tg)


async def test_shadow_never_applies_to_a_trial_user(engine):
    """Bepul is enforced from day one. Shadowing it would leave the free tier
    exactly as unmetered as it is today, against zero revenue."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm, uses_left=1, monthly_variant_limit=None, monthly_check_limit=1)
    try:
        assert await quota.check_and_consume(sm, tg, quota.CHECK, shadow=True) is True
        assert await quota.check_and_consume(sm, tg, quota.CHECK, shadow=True) is False, (
            "shadow mode leaked into the trial tier")
    finally:
        await _drop(sm, tg)


async def test_can_add_book_shadow_logs_instead_of_blocking(engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.book import Book
    from app.services import quota
    from tests.test_book_models import _mk_book

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm, monthly_variant_limit=20, book_limit=1, uses_left=None)
    row = await _read(sm, tg)
    spy = _LogSpy()
    monkeypatch.setattr(quota, "logger", spy)
    try:
        await _mk_book(sm, row.id, themes=0)
        assert await quota.can_add_book(sm, tg, shadow=False) is False
        assert await quota.can_add_book(sm, tg, shadow=True) is True
        assert "quota_would_block" in spy.names()
    finally:
        from sqlalchemy import delete
        async with sm() as s:
            await s.execute(delete(Book).where(Book.user_id == row.id))
            await s.commit()
        await _drop(sm, tg)


def test_shadow_mode_default_is_on_for_the_first_cycle():
    """One cycle of would-have-blocked data, then a decision. If this flips to
    False without the cycle being reviewed, the caps were guessed twice."""
    assert settings.QUOTA_SHADOW_MODE is True


# ── 5. Bepul: public library only, no upload ─────────────────────────────────

def _trial_user(tg=7777):
    return SimpleNamespace(
        id="uid", telegram_id=tg, is_admin=False, uses_left=1,
        is_blocked=False, access_until=None, monthly_variant_limit=None,
        language=SimpleNamespace(value="uz"),
    )


def _paid_user(tg=8888):
    return SimpleNamespace(
        id="uid", telegram_id=tg, is_admin=False, uses_left=None,
        is_blocked=False, access_until=None, monthly_variant_limit=20,
        language=SimpleNamespace(value="uz"),
    )


class _BotMsg:
    """_Msg plus the `.bot` a real aiogram Message carries — the trial charge
    goes through access.charge_single_and_notify(message.bot, ...), the same
    way upload.py:875 does it."""

    def __init__(self):
        from tests.test_quota_not_wasted import _Msg
        self._inner = _Msg()
        self.bot = SimpleNamespace()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_upload_button_hidden_for_trial_shown_for_paid():
    from app.bot.keyboards.inline import book_entry_menu_keyboard

    def labels(markup):
        return [b.text for row in markup.inline_keyboard for b in row]

    paid = book_entry_menu_keyboard("uz", has_books=True, has_public=True, can_upload=True)
    trial = book_entry_menu_keyboard("uz", has_books=True, has_public=True, can_upload=False)

    assert any("Kitob qo'shish" in t for t in labels(paid))
    assert not any("Kitob qo'shish" in t for t in labels(trial))
    # the trial user keeps every READ path, including books already granted
    # to them from the public library
    assert any("Umumiy kutubxona" in t for t in labels(trial))
    assert any("Eski kitoblardan" in t for t in labels(trial))


async def test_trial_book_add_is_refused_with_a_clear_message():
    """Never a silent block: the refusal must name the reason AND the way out
    (the public library), not just say 'limit reached'."""
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import _CB, _Msg, _State

    msg = _Msg()
    state = _State()
    await book.handle_book_add(_CB("book:add", msg), state, _trial_user())

    assert msg.answers, "trial upload was blocked silently"
    said = msg.answers[0]
    assert "kutubxona" in said.lower()          # points at the public library
    assert state.state is None, "a refused upload must not start the title flow"


async def test_paid_book_add_still_opens_the_title_flow(monkeypatch):
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import _CB, _Msg, _State

    async def _can_add(*a, **kw):
        return True

    monkeypatch.setattr(book.quota, "can_add_book", _can_add)
    msg = _Msg()
    state = _State()
    await book.handle_book_add(_CB("book:add", msg), state, _paid_user())

    assert state.state == book.BookStates.waiting_for_title


async def test_trial_keeps_generating_from_books_it_already_owns(monkeypatch):
    """The ownership framing, not the menu: downgrading (or never upgrading)
    must not retro-lock books already in the library, including public-library
    books granted through book_access."""
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import _CB, _Msg, _State

    shown = []

    async def _picker(message, state, user_id, lang):
        shown.append(user_id)
        return True

    monkeypatch.setattr(book, "_show_book_picker", _picker)
    await book.handle_book_library(_CB("book:library", _Msg()), _State(), _trial_user())

    assert shown == ["uid"], "a trial user was blocked from their own library"


# ── 6. The book generation path ──────────────────────────────────────────────

async def test_book_generation_consumes_the_shared_test_kind(monkeypatch):
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import (
        _CB, _Msg, _gen_state, _noop, _ok_generation, _user,
    )
    from app.services import quota

    consumed = []

    async def _consume(session_factory, tg, kind):
        consumed.append(kind)
        return True

    monkeypatch.setattr(book.quota, "check_and_consume", _consume)
    monkeypatch.setattr(book, "_run_generation", _ok_generation)
    monkeypatch.setattr(book, "_show_generation_review", _noop)

    await book.handle_book_images_choice(_CB("bookimg:no", _Msg()), _gen_state(), _user())

    assert consumed == [quota.TEST], (
        "book generation must draw from the shared pool, not its own meter")


async def test_trial_generation_charges_the_lifetime_use(monkeypatch):
    """The hole the audit found: book.py never called decrement_use, so a Bepul
    user had unlimited book generation. One use, charged once, shared with the
    upload path because it is the same counter."""
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import _CB, _gen_state, _noop, _ok_generation

    charged = []

    async def _consume(session_factory, tg, kind):
        return True

    async def _charge(bot, db_user):
        charged.append(db_user.telegram_id)
        return 0

    monkeypatch.setattr(book.quota, "check_and_consume", _consume)
    monkeypatch.setattr(book.access, "charge_single_and_notify", _charge)
    monkeypatch.setattr(book, "_run_generation", _ok_generation)
    monkeypatch.setattr(book, "_show_generation_review", _noop)

    await book.handle_book_images_choice(
        _CB("bookimg:no", _BotMsg()), _gen_state(), _trial_user())

    assert charged == [7777], "a trial book generation was free"


async def test_exhausted_trial_is_blocked_before_any_paid_call(monkeypatch):
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import _CB, _Msg, _gen_state, _noop

    generated = []
    charged = []

    async def _consume(session_factory, tg, kind):
        return True

    async def _charge(bot, db_user):
        charged.append(1)
        return None

    async def _gen(*a, **kw):
        generated.append(1)
        return ([], [], [], [])

    monkeypatch.setattr(book.quota, "check_and_consume", _consume)
    monkeypatch.setattr(book.access, "charge_single_and_notify", _charge)
    monkeypatch.setattr(book, "_run_generation", _gen)
    monkeypatch.setattr(book, "_show_generation_review", _noop)

    spent = _trial_user()
    spent.uses_left = 0
    state = _gen_state()
    await book.handle_book_images_choice(_CB("bookimg:no", _Msg()), state, spent)

    assert generated == [], "an exhausted trial still paid for a Gemini generation"
    assert charged == [], "an exhausted trial was charged again"
    assert state.cleared


async def test_page_range_peeks_before_the_paid_extraction(monkeypatch):
    """upload.py:812 peeks before its paid extraction; the book path had the
    consume half of that pattern and not the peek half, so an out-of-quota
    teacher waited through (and was billed for) full Vision extraction before
    being told no."""
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import _Msg, _State

    read = []

    async def _no_quota(session_factory, tg, kind):
        return False

    async def _read_file(*a, **kw):
        read.append(1)
        return b"%PDF-"

    monkeypatch.setattr(book.quota, "check_available", _no_quota)
    monkeypatch.setattr(book.storage, "read_file", _read_file)

    msg = _Msg()
    state = _State()
    bookrow = SimpleNamespace(
        id="bid", title="B", page_count=100, original_pdf_path="p.pdf",
        language="uz", kind=None,
    )
    await book._run_page_range_generation(
        msg, state, _paid_user(), bookrow, [(1, 5)], 0)

    assert read == [], "the PDF was read (and Vision would have run) with no quota"
    assert msg.answers, "the teacher was told nothing"
