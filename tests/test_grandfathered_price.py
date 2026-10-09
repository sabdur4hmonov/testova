"""What a teacher PAID, kept apart from what a plan COSTS today.

THE BUG. Pricing moved Standart 50,000 -> 75,000 and Pro 100,000 -> 150,000
(commit 3572928). `price_som` is a constant on a frozen dataclass, not a
column, and account.summary_lines derived the tier from the user's limits and
then printed today's constant. So a teacher who paid 50,000 opened "Hisobim"
and was told their plan costs 75,000.

Nothing is CHARGED by this - payment is manual, the admin assigns a plan with
/plan, and no code path moves money. The damage is entirely in what the account
page tells an existing customer they are paying.

THE SHAPE OF THE FIX, and its deliberate limits:

  * The stamp is written by /plan ONLY, because /plan is the sale. Renewal
    (/resetquota) leaves it alone, exactly as it already leaves the limits
    alone - so a grandfathered price holds indefinitely and only an explicit
    re-sale moves it.
  * NULL means "sold before this column existed". Those rows fall back to the
    live constant, which is the honest answer: nothing in the schema records
    what they actually paid (period_start is rewritten by every renewal, so it
    cannot date a purchase). Re-running /plan on such a user is how an admin
    grandfathers them, and it is a decision rather than a guess.
  * The pricing MENU is untouched. It advertises what a plan costs TODAY and
    must keep doing so; only the personalised account line is per-teacher.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.admin_log import AdminLog
from app.services import account, plans

from tests.synthetic_ids import TEST_ADMIN_ID


def _paid(**over):
    """A paid user. Deliberately built with SimpleNamespace and WITHOUT the new
    attribute unless a test adds it — that is what a pre-migration ORM row and
    every existing fixture look like, and the read must survive both."""
    now = datetime.now(timezone.utc)
    d = dict(
        monthly_variant_limit=20, variant_count_this_period=0,
        monthly_check_limit=400, check_count_this_period=0,
        period_start=now, access_until=now + timedelta(days=30),
        uses_left=None, is_admin=False,
    )
    d.update(over)
    return SimpleNamespace(**d)


# ── The account line ─────────────────────────────────────────────────────────

def test_a_stamped_price_is_what_the_teacher_is_shown():
    """The whole point: this teacher bought Standart at the old price and must
    keep being told 50,000, even though Standart now costs 75,000."""
    lines = "\n".join(account.summary_lines(_paid(price_som_at_purchase=50_000)))
    assert "50,000 so'm/oy" in lines
    assert "75,000" not in lines
    assert "Standart" in lines          # the TIER is still derived from limits


def test_a_row_with_no_stamp_falls_back_to_todays_price():
    """Sold before the column existed. Nothing records what they paid, so the
    live constant is the only honest answer — a guessed number would be worse."""
    lines = "\n".join(account.summary_lines(_paid()))
    assert "75,000 so'm/oy" in lines


def test_a_null_stamp_falls_back_the_same_way():
    """The ORM shape of the same case: the attribute EXISTS and is None."""
    lines = "\n".join(account.summary_lines(_paid(price_som_at_purchase=None)))
    assert "75,000 so'm/oy" in lines


def test_the_stamp_never_invents_a_price_for_bepul():
    """Bepul shows no price line at all. A stray stamp on a free row (an admin
    who cleared the limits after a sale) must not resurrect one."""
    now = datetime.now(timezone.utc)
    free = SimpleNamespace(
        monthly_variant_limit=None, variant_count_this_period=0,
        monthly_check_limit=None, check_count_this_period=0,
        period_start=None, access_until=now + timedelta(days=10),
        uses_left=1, is_admin=False, price_som_at_purchase=50_000,
    )
    lines = "\n".join(account.summary_lines(free))
    assert "Bepul" in lines
    assert "Narx" not in lines
    assert "50,000" not in lines


def test_a_legacy_topped_up_row_keeps_its_stamp():
    """A legacy 25/500 row still derives as Standart (`_tier_by` is >=), and the
    stamp must ride that unchanged — this is precisely the cohort that paid the
    old price."""
    user = _paid(monthly_variant_limit=25, monthly_check_limit=500,
                 price_som_at_purchase=50_000)
    assert plans.plan_name(user).startswith("Standart")
    assert "50,000 so'm/oy" in "\n".join(account.summary_lines(user))


def test_the_pricing_menu_still_advertises_current_prices():
    """The menu is not personalised: it reads the plan constants directly and
    must keep showing what a plan costs TODAY, whatever any user row says."""
    assert plans.STANDART.price_som == 75_000
    assert plans.PRO.price_som == 150_000


# ── Who writes the stamp ─────────────────────────────────────────────────────

async def test_set_plan_stamps_the_price_that_was_sold(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            u = await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "standart")
        assert u.price_som_at_purchase == plans.STANDART.price_som
        assert u.monthly_variant_limit == plans.STANDART.test_limit

        # An upgrade is a NEW sale, so it re-stamps.
        async with sm() as s:
            u = await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "pro")
        assert u.price_som_at_purchase == plans.PRO.price_som
    finally:
        async with sm() as s:
            # admin_log.target has no FK, so deleting the User never reaches it
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_renewal_does_not_move_a_grandfathered_price(engine):
    """/resetquota is a RENEWAL, not a sale. It already leaves the tier and both
    limits alone; the stamp rides with them, so a grandfathered teacher stays
    grandfathered across every renewal until an admin re-sells them."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    old = datetime.now(timezone.utc) - timedelta(days=25)
    try:
        async with sm() as s:
            s.add(User(
                telegram_id=tg, full_name="Grandfathered",
                monthly_variant_limit=20, monthly_check_limit=400,
                variant_count_this_period=20, check_count_this_period=400,
                period_start=old, access_until=old + timedelta(days=30),
                uses_left=None, price_som_at_purchase=50_000,
            ))
            await s.commit()

        async with sm() as s:
            u = await admin_users.reset_quota(s, TEST_ADMIN_ID, tg)

        assert u.price_som_at_purchase == 50_000, "renewal must not re-price"
        assert u.variant_count_this_period == 0      # it IS a real renewal
        assert u.monthly_variant_limit == 20
        assert "50,000 so'm/oy" in "\n".join(account.summary_lines(u))
    finally:
        async with sm() as s:
            # admin_log.target has no FK, so deleting the User never reaches it
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_an_existing_row_migrates_to_null_and_nobody_is_repriced(engine):
    """ADDITIVE: the column is nullable with no backfill, so a row created
    without it reads NULL and falls back — no existing customer's displayed
    price changes as a side effect of deploying this."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            s.add(User(
                telegram_id=tg, full_name="Pre-existing",
                monthly_variant_limit=20, monthly_check_limit=400,
                uses_left=None,
            ))
            await s.commit()
        async with sm() as s:
            u = await admin_users.get_or_create(s, tg)
        assert u.price_som_at_purchase is None
        assert "75,000 so'm/oy" in "\n".join(account.summary_lines(u))
    finally:
        async with sm() as s:
            # admin_log.target has no FK, so deleting the User never reaches it
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


# ── /setprice — the only way to record what was ACTUALLY paid ────────────────
#
# WHY A SEPARATE COMMAND, and not an argument on /plan. set_plan stamps
# plan.price_som, which is TODAY's constant — so re-running /plan on a teacher
# who paid 50,000 writes 75,000 and destroys the NULL that was at least falling
# back honestly. And /plan is not a price-only operation: it also zeroes both
# period counters, restarts period_start and pushes access_until out 30 days.
# Correcting a price through it would hand the customer a free quota reset and a
# month of access.
#
# So the price gets its own setter that touches the price and NOTHING else.
# That isolation is the entire reason this command exists, and the test below
# pins it field by field.

async def test_set_price_records_what_was_paid(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "standart")
        # sold at the old price; the sale stamped today's 75,000
        async with sm() as s:
            u = await admin_users.set_price(s, TEST_ADMIN_ID, tg, 50_000)
        assert u.price_som_at_purchase == 50_000
        assert "50,000 so'm/oy" in "\n".join(account.summary_lines(u))
    finally:
        async with sm() as s:
            # admin_log.target has no FK, so deleting the User never reaches it
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_set_price_minus_one_clears_back_to_the_fallback(engine):
    """Matching the -1 convention every other setter uses. Here it means "forget
    what I recorded", not "unlimited" — so a mis-typed price can be undone to the
    honest fallback rather than being stuck as another wrong number."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "standart")
        async with sm() as s:
            await admin_users.set_price(s, TEST_ADMIN_ID, tg, 50_000)
        async with sm() as s:
            u = await admin_users.set_price(s, TEST_ADMIN_ID, tg, -1)
        assert u.price_som_at_purchase is None
        assert "75,000 so'm/oy" in "\n".join(account.summary_lines(u))
    finally:
        async with sm() as s:
            # admin_log.target has no FK, so deleting the User never reaches it
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_set_price_touches_the_price_and_nothing_else(engine):
    """THE reason this is a separate command. Every quota/access field must come
    out byte-identical — correcting what a teacher paid must never hand them a
    fresh billing period, a zeroed counter, or an extra month of access."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    old = datetime.now(timezone.utc) - timedelta(days=25)
    try:
        async with sm() as s:
            s.add(User(
                telegram_id=tg, full_name="Mid-period",
                monthly_variant_limit=20, monthly_check_limit=400, book_limit=4,
                variant_count_this_period=17, check_count_this_period=300,
                period_start=old, access_until=old + timedelta(days=30),
                uses_left=None, is_blocked=False,
            ))
            await s.commit()

        watched = ("variant_count_this_period", "check_count_this_period",
                   "period_start", "access_until", "monthly_variant_limit",
                   "monthly_check_limit", "book_limit", "uses_left",
                   "is_blocked")
        async with sm() as s:
            before = await admin_users.get_or_create(s, tg)
            snapshot = {f: getattr(before, f) for f in watched}

        async with sm() as s:
            after = await admin_users.set_price(s, TEST_ADMIN_ID, tg, 50_000)

        assert after.price_som_at_purchase == 50_000
        for field in watched:
            assert getattr(after, field) == snapshot[field], (
                f"set_price moved {field}: {snapshot[field]} -> "
                f"{getattr(after, field)}"
            )
    finally:
        async with sm() as s:
            # admin_log.target has no FK, so deleting the User never reaches it
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_set_price_is_logged_like_every_other_admin_action(engine):
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            await admin_users.set_price(s, TEST_ADMIN_ID, tg, 50_000)
        async with sm() as s:
            actions = (await s.execute(
                select(AdminLog.action).where(AdminLog.target == tg)
            )).scalars().all()
        assert "setprice" in actions
    finally:
        async with sm() as s:
            # admin_log.target has no FK, so deleting the User never reaches it
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


# ── The admin has to be able to read the value back ─────────────────────────

def test_fmt_user_shows_the_recorded_price():
    """Setting a value you cannot read back is how the next wrong number goes
    unnoticed. /plan, /user, /users and /find all render through _fmt_user."""
    from app.bot.handlers.admin import _fmt_user

    user = _admin_view_user(price_som_at_purchase=50_000)
    assert "50,000" in _fmt_user(user)


def test_fmt_user_shows_a_dash_when_no_price_was_recorded():
    from app.bot.handlers.admin import _fmt_user

    out = _fmt_user(_admin_view_user(price_som_at_purchase=None))
    assert "To'langan" in out or "To‘langan" in out


def _admin_view_user(**over):
    now = datetime.now(timezone.utc)
    d = dict(
        telegram_id=12345, full_name="Teacher", username=None, note=None,
        uses_left=None, access_until=now + timedelta(days=20),
        monthly_variant_limit=20, monthly_check_limit=400, book_limit=4,
        monthly_book_analysis_limit=1, monthly_book_generation_limit=5,
        variant_count_this_period=0, check_count_this_period=0,
        book_analysis_count_this_period=0, book_generation_count_this_period=0,
        is_blocked=False, total_projects=0, updated_at=now,
    )
    d.update(over)
    return SimpleNamespace(**d)
