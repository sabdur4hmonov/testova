"""
HTTP-level tests for the admin panel: every GET renders, and the auth/CSRF
gates actually gate.

WHY THIS FILE EXISTS. `dashboard.py` read a `Plan` field that had been renamed
out from under it, so GET / raised AttributeError on every load — the landing
page after login, broken for seven days. Nothing caught it because nothing in
this suite had ever issued an HTTP request at the panel: the only panel tests
were unit tests of `rate_limit` and `security`. A route that renders is the
cheapest possible assertion and it is the one that was missing.

CLIENT CHOICE. httpx.AsyncClient over an ASGITransport, not starlette's
TestClient. Both drive the app in-process through the full middleware stack;
the difference is the event loop. TestClient runs the app on its own portal
thread/loop, so the asyncpg connections held by the `session` fixture (created
on pytest-asyncio's loop) fail with "attached to a different loop" the moment a
route touches the DB. AsyncClient runs on the SAME loop as the test, so the
seeded fixture session IS the session the routes use.

DB. The `engine`/`session` fixtures from conftest (testova_test, NullPool,
create_all/drop_all per test) — the pattern test_subscription.py already uses.
Importing `app.models` registers every table on Base.metadata so create_all
builds the whole schema, not just the models conftest happens to import.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 — registers EVERY table on Base.metadata
from app.admin.web import rate_limit, security
from app.admin.web.app import app as panel_app
from app.admin.web.services import monitoring
from app.config import settings
from app.database import get_session
from app.models.admin_log import AdminLog
from app.models.billing_reconciliation import BillingReconciliation
from app.models.book import Book
from app.models.credit_baseline import CreditBaseline
from app.models.gemini_usage import GeminiUsage
from app.models.user import User

PANEL_PASSWORD = "panel-test-password"
# Hashed ONCE at import. bcrypt costs ~0.5s a call by design, and the client
# fixture runs per test; hashing per test spent more wall-clock on key
# stretching than on the assertions.
PANEL_PASSWORD_HASH = security.hash_password(PANEL_PASSWORD)

# Telegram ids used by the seed, so a test can address a known row.
TG_PAID = 100_001
TG_TRIAL = 100_002
TG_ADMIN = 100_003
TG_BLOCKED = 100_004
TG_LEGACY = 100_005


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> dict:
    """One row of every shape the panel's pages branch on.

    Deliberately includes an ADMIN with a live trial meter and a LEGACY row with
    neither limit nor meter: those are the two rows plans.is_trial() excludes,
    and the dashboard's trial count is only meaningful if they are present to be
    excluded.
    """
    now = datetime.now(timezone.utc)

    paid = User(telegram_id=TG_PAID, username="paid_teacher", full_name="Paid Teacher",
                monthly_variant_limit=20, monthly_check_limit=400, book_limit=4,
                access_until=now + timedelta(days=20), total_projects=3)
    trial = User(telegram_id=TG_TRIAL, username="trial_teacher", full_name="Trial Teacher",
                 monthly_variant_limit=None, uses_left=1, total_projects=0)
    admin = User(telegram_id=TG_ADMIN, username="the_admin", full_name="The Admin",
                 is_admin=True, uses_left=1)
    blocked = User(telegram_id=TG_BLOCKED, username="blocked_teacher",
                   full_name="Blocked Teacher", monthly_variant_limit=20, is_blocked=True)
    legacy = User(telegram_id=TG_LEGACY, full_name="Legacy Row")
    session.add_all([paid, trial, admin, blocked, legacy])
    await session.flush()

    public_book = Book(user_id=paid.id, title="Public Textbook", subject="Matematika",
                       grade="7", page_count=120, original_pdf_path="books/public.pdf",
                       pdf_checksum="c0ffee00", is_public=True,
                       uploaded_by_user_id=TG_PAID)
    private_book = Book(user_id=paid.id, title="Private Textbook",
                        original_pdf_path="books/private.pdf", pdf_checksum="deadbeef",
                        is_public=False, uploaded_by_user_id=TG_PAID)
    session.add_all([public_book, private_book])

    # Two models and one unattributed row, so the cost pages exercise per-model
    # grouping and the NULL-user fold.
    session.add_all([
        GeminiUsage(user_id=TG_PAID, kind="extract", model="gemini-2.5-flash",
                    prompt_tokens=1_000_000, output_tokens=100_000,
                    thinking_tokens=0, total_tokens=1_100_000, created_at=now),
        GeminiUsage(user_id=TG_PAID, kind="book_toc", model="gemini-3-flash",
                    prompt_tokens=500_000, output_tokens=50_000,
                    thinking_tokens=10_000, total_tokens=560_000, created_at=now),
        GeminiUsage(user_id=None, kind="grade", model="gemini-2.5-flash",
                    prompt_tokens=200_000, output_tokens=20_000,
                    thinking_tokens=0, total_tokens=220_000, created_at=now),
    ])

    session.add(AdminLog(admin_id=TG_ADMIN, action="grant", target=TG_PAID,
                         params={"days": 30}))
    session.add(CreditBaseline(credit_usd=50.0, as_of=now - timedelta(days=1),
                               note="seed", created_by=TG_ADMIN))
    await session.flush()
    return {"paid": paid, "trial": trial, "admin": admin,
            "blocked": blocked, "legacy": legacy,
            "public_book": public_book, "private_book": private_book}


@pytest_asyncio.fixture
async def client(session: AsyncSession, monkeypatch, tmp_path):
    """Anonymous panel client. Routes run against the test session."""
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", PANEL_PASSWORD_HASH)
    monkeypatch.setattr(settings, "ADMIN_SESSION_SECRET", "test-session-secret")
    # The log tail is a dev-machine artifact (a real multi-MB testova.log sits in
    # the repo). Point it at nothing so /alerts is hermetic and fast.
    monkeypatch.setattr(monitoring, "LOG_FILE", tmp_path / "absent.log")
    rate_limit.reset()
    from app.admin.web import send_guard
    send_guard.reset_for_testing()
    # The summary is memoized for 60s (AUDIT C-29) and LOG_FILE is patched per
    # test, so without this a cached answer computed against one test's log
    # path would be served to the next.
    monitoring.reset_cache()

    async def _override():
        yield session

    panel_app.dependency_overrides[get_session] = _override

    # /healthz bypasses get_session on purpose and opens the MODULE-LEVEL
    # app.database.async_session_factory — bound to the live testova_db. Point
    # that factory at this test's session too (the connection guard in
    # tests/live_db_guard.py caught /healthz reading the live database), and
    # count its uses so the "real engine, not a request override" test can prove
    # which path the route took.
    from app import database

    module_factory_calls: list[bool] = []

    class _ModuleFactorySession:
        async def __aenter__(self):
            module_factory_calls.append(True)
            return session

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(database, "async_session_factory", lambda: _ModuleFactorySession())

    # base_url MUST be https: the session cookie is https_only, so over http the
    # client silently drops it and every "logged in" request is anonymous.
    async with AsyncClient(transport=ASGITransport(app=panel_app),
                           base_url="https://testserver") as c:
        c.module_factory_calls = module_factory_calls
        yield c
    panel_app.dependency_overrides.clear()
    rate_limit.reset()
    monitoring.reset_cache()


@pytest_asyncio.fixture
async def auth_client(client):
    """Logged-in client, through the real login POST rather than a forged cookie."""
    r = await client.post("/login", data={"username": settings.ADMIN_PANEL_USERNAME,
                                          "password": PANEL_PASSWORD})
    assert r.status_code == 303, "login fixture failed to authenticate"
    return client


def _csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "no csrf_token hidden field in the rendered page"
    return m.group(1)


# ── 1. every GET renders ─────────────────────────────────────────────────────

# Every GET route the panel serves. Keep in step with app.routes — the point of
# the list is that a route which stops rendering fails HERE, loudly, rather than
# on the admin's screen a week later.
GET_ROUTES = [
    "/",
    "/users",
    "/users?q=teacher",
    "/users?page=2",
    "/costs",
    "/books",
    "/books/library",
    "/alerts",
    "/audit",
    "/audit?action=grant",
    "/export/usage.csv",
    "/export/user-costs.csv",
    "/export/audit.csv",
]


async def test_every_get_route_renders(auth_client, seeded):
    """One test, every route, ALL failures reported.

    Deliberately not parametrized. The conftest `engine` fixture builds and
    drops the whole schema per test (~4s), so 13 parameters cost 13 setups for
    13 identical seeds. Collecting instead of asserting per route also means a
    change that breaks four pages names all four in one run, rather than hiding
    three behind the first.
    """
    broken = []
    for path in GET_ROUTES:
        r = await auth_client.get(path)
        if r.status_code != 200 or not r.content:
            broken.append(f"{path} -> {r.status_code}: {r.text[:300]}")
    assert not broken, "routes that did not render:\n" + "\n".join(broken)


async def test_user_detail_renders_for_every_seeded_user(auth_client, seeded):
    for tg in (TG_PAID, TG_TRIAL, TG_ADMIN, TG_BLOCKED, TG_LEGACY):
        r = await auth_client.get(f"/users/{tg}")
        assert r.status_code == 200, f"/users/{tg} -> {r.status_code}\n{r.text[:500]}"


async def test_user_detail_for_an_unknown_id_redirects_not_500s(auth_client, seeded):
    r = await auth_client.get("/users/987654321", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/users"


async def test_dashboard_counts_trial_users_by_is_trial(auth_client, seeded):
    """THE regression guard for P1.

    The seed holds five users; plans.is_trial() admits exactly one — the admin
    is excluded despite a live meter, the legacy row for having none, and the
    paid/blocked rows hold a paid pool. The old SQL predicate also demanded
    total_projects > 0, which the trial row does not have, so it counted 0.
    """
    from app.services.plans import is_trial

    expected = sum(1 for u in seeded.values() if isinstance(u, User) and is_trial(u))
    assert expected == 1, "seed no longer has exactly one trial user"

    r = await auth_client.get("/")
    assert r.status_code == 200
    m = re.search(r'Sinov \(trial\).*?display-6 fw-semibold">(\d+)<', r.text, re.S)
    assert m, "trial stat tile missing from the dashboard"
    assert int(m.group(1)) == expected


async def test_dashboard_counts_active_paid_excluding_blocked(auth_client, seeded):
    """The paid tile reads Plan.test_limit. Two seeded users sit at the Standart
    pool and one of them is blocked, so the answer is 1 — and a field-name break
    is a 500 here, not a quietly wrong integer."""
    r = await auth_client.get("/")
    assert r.status_code == 200
    m = re.search(r'Faol obunalar.*?display-6 fw-semibold">(\d+)<', r.text, re.S)
    assert m, "active-paid stat tile missing from the dashboard"
    assert int(m.group(1)) == 1


async def test_csv_exports_carry_a_header_and_attachment_disposition(auth_client, seeded):
    for path, first_col in [("/export/usage.csv", "created_at"),
                            ("/export/user-costs.csv", "telegram_id"),
                            ("/export/audit.csv", "created_at")]:
        r = await auth_client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers.get("content-disposition", "")
        assert r.text.splitlines()[0].startswith(first_col)


# ── 2. auth / CSRF matrix ────────────────────────────────────────────────────

PROTECTED_GETS = ["/", "/users", f"/users/{TG_PAID}", "/costs", "/books",
                  "/books/library", "/revenue", "/messaging", "/alerts", "/audit",
                  "/export/usage.csv", "/export/user-costs.csv", "/export/audit.csv"]


async def test_unauthenticated_get_redirects_to_login(client):
    leaked = []
    for path in PROTECTED_GETS:
        r = await client.get(path, follow_redirects=False)
        expected = f"/login?next={path.split('?')[0]}"
        if r.status_code != 303 or r.headers.get("location") != expected:
            leaked.append(f"{path} -> {r.status_code} {r.headers.get('location')!r}")
    assert not leaked, "reachable without logging in:\n" + "\n".join(leaked)


async def test_wrong_password_is_401_and_sets_no_session(client):
    r = await client.post("/login", data={"username": settings.ADMIN_PANEL_USERNAME,
                                          "password": "not-the-password"})
    assert r.status_code == 401
    assert "testova_admin" not in client.cookies
    after = await client.get("/", follow_redirects=False)
    assert after.status_code == 303


async def test_wrong_username_is_also_401(client):
    r = await client.post("/login", data={"username": "not-the-admin",
                                          "password": PANEL_PASSWORD})
    assert r.status_code == 401


async def test_correct_password_is_303_and_sets_the_session(client):
    r = await client.post("/login", data={"username": settings.ADMIN_PANEL_USERNAME,
                                          "password": PANEL_PASSWORD})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "testova_admin" in client.cookies


async def test_login_honours_a_relative_next_but_not_an_open_redirect(client):
    r = await client.post("/login", data={"username": settings.ADMIN_PANEL_USERNAME,
                                          "password": PANEL_PASSWORD,
                                          "next": "//evil.example.com/"})
    assert r.status_code == 303
    assert r.headers["location"] == "/", "next= was usable as an open redirect"


# Every mutating POST, with a body that would succeed if CSRF were not enforced.
POST_ROUTES = [
    ("/logout", {}),
    (f"/users/{TG_PAID}/grant", {"days": "30", "uses": "5"}),
    (f"/users/{TG_PAID}/extend", {"days": "7"}),
    (f"/users/{TG_PAID}/setuses", {"n": "9"}),
    (f"/users/{TG_PAID}/setbooklimit", {"n": "6"}),
    (f"/users/{TG_PAID}/revoke", {}),
    (f"/users/{TG_PAID}/unblock", {}),
    (f"/books/{TG_PAID}/setbooklimit", {"n": "6"}),
    ("/costs/balance", {"credit_usd": "10.0"}),
    ("/costs/reconcile", {"google_total_usd": "1.0"}),
    (f"/users/{TG_PAID}/payment", {"plan": "pro", "amount_som": "150000"}),
    ("/messaging/broadcast", {"segment": "paid_active", "text": "hi"}),
    (f"/users/{TG_PAID}/message", {"text": "hi"}),
]


async def test_post_without_csrf_token_is_403(auth_client, seeded):
    accepted = []
    for path, body in POST_ROUTES:
        r = await auth_client.post(path, data=body, follow_redirects=False)
        if r.status_code != 403:
            accepted.append(f"{path} -> {r.status_code}")
    assert not accepted, "accepted a POST with no csrf_token:\n" + "\n".join(accepted)


async def test_post_with_a_bogus_csrf_token_is_403(auth_client, seeded):
    accepted = []
    for path, body in POST_ROUTES:
        r = await auth_client.post(path, data={**body, "csrf_token": "not-the-token"},
                                   follow_redirects=False)
        if r.status_code != 403:
            accepted.append(f"{path} -> {r.status_code}")
    assert not accepted, "accepted a forged csrf_token:\n" + "\n".join(accepted)


async def test_post_with_a_stale_csrf_token_is_403(client, seeded):
    """A token minted in one session must not work in the next. Logging out
    drops the session token; logging back in mints a fresh one."""
    await client.post("/login", data={"username": settings.ADMIN_PANEL_USERNAME,
                                      "password": PANEL_PASSWORD})
    page = await client.get(f"/users/{TG_PAID}")
    stale = _csrf_from(page.text)

    await client.post("/logout", data={"csrf_token": stale})
    await client.post("/login", data={"username": settings.ADMIN_PANEL_USERNAME,
                                      "password": PANEL_PASSWORD})

    r = await client.post(f"/users/{TG_PAID}/extend",
                          data={"days": "7", "csrf_token": stale},
                          follow_redirects=False)
    assert r.status_code == 403, "a token from a previous session was accepted"


async def test_a_valid_csrf_token_is_accepted(auth_client, seeded):
    """The 403s above have to mean "CSRF rejected", not "this endpoint is
    broken" — so prove the same POST succeeds with the real token."""
    page = await auth_client.get(f"/users/{TG_PAID}")
    token = _csrf_from(page.text)
    r = await auth_client.post(f"/users/{TG_PAID}/extend",
                               data={"days": "7", "csrf_token": token},
                               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/users/{TG_PAID}"


async def test_unauthenticated_post_never_reaches_the_handler(client, seeded):
    """No session means no session CSRF token, so a mutating POST is refused
    rather than reaching the handler."""
    r = await client.post(f"/users/{TG_PAID}/revoke", data={"csrf_token": "x"},
                          follow_redirects=False)
    assert r.status_code in (303, 403)
    assert r.status_code != 200


async def test_logging_in_starts_a_fresh_session(client, seeded):
    """A login starts from an empty session, not from whatever the old cookie
    held.

    Starlette's session is a SIGNED COOKIE with no server-side id, so classic
    fixation does not apply — an attacker cannot mint a valid cookie without
    the secret. What CAN regress is state surviving a logout/login boundary:
    `logout()` clears only the auth marker and the CSRF token, so any other key
    (a flash queued but never displayed) would otherwise be shown to the next
    session that logs in.
    """
    await client.post("/login", data={"username": settings.ADMIN_PANEL_USERNAME,
                                      "password": PANEL_PASSWORD})
    token = _csrf_from((await client.get(f"/users/{TG_PAID}")).text)

    # Queue a flash and deliberately do NOT follow the redirect that consumes it.
    r = await client.post(f"/users/{TG_PAID}/extend",
                          data={"days": "7", "csrf_token": token},
                          follow_redirects=False)
    assert r.status_code == 303

    await client.post("/logout", data={"csrf_token": token})
    await client.post("/login", data={"username": settings.ADMIN_PANEL_USERNAME,
                                      "password": PANEL_PASSWORD})

    body = (await client.get("/users")).text
    assert "Muddat uzaytirildi" not in body, (
        "a flash queued before logout leaked into the next session"
    )


# ── 3. /healthz ──────────────────────────────────────────────────────────────

async def test_healthz_needs_no_login(client):
    """The one route an orchestrator/proxy/uptime monitor hits BEFORE an admin
    has ever logged in, so it must not be behind require_login."""
    r = await client.get("/healthz", follow_redirects=False)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_healthz_sets_no_session_cookie(client):
    """An uptime monitor polling every 30s should not accumulate session state
    — the route never touches request.session, so SessionMiddleware has
    nothing to persist."""
    r = await client.get("/healthz")
    assert "testova_admin" not in r.cookies


async def test_healthz_degrades_cleanly_and_leaks_nothing_when_the_db_is_down(
    client, monkeypatch
):
    """Simulates a DB outage without touching the real database: swap in a
    session factory that raises on __aenter__, the same shape
    async_session_factory() normally returns.

    The response must degrade to a fixed, minimal body — no stack trace, no
    exception text, no hint of what broke. That is the same discipline the
    openapi.json lockdown was about, applied to a route that (unlike the
    schema) is SUPPOSED to be reachable without a login.
    """
    from app import database

    class _BrokenSession:
        async def __aenter__(self):
            raise ConnectionError(
                "simulated: could not connect to server: Connection refused"
            )

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(database, "async_session_factory", lambda: _BrokenSession())

    r = await client.get("/healthz", follow_redirects=False)
    assert r.status_code == 503
    assert r.json() == {"status": "degraded"}
    assert "simulated" not in r.text
    assert "ConnectionError" not in r.text
    assert "Connection refused" not in r.text
    assert "Traceback" not in r.text


async def test_healthz_degrades_even_when_logging_the_failure_itself_raises(
    client, monkeypatch
):
    """PROVEN AGAINST A REAL FAILURE, not a hypothetical: with the real
    testova-pg container stopped, the first version of this route 500'd —
    not because the DB check failed to catch the outage, but because the
    logger.warning(..., exc_info=True) call made INSIDE the except block
    itself raised (structlog's console handler here has no encoding fallback,
    and the real asyncpg traceback contained a character the console's
    codepage couldn't encode). A health endpoint 500ing because its OWN
    failure-reporting failed is worse than doing nothing.

    Reproduced deterministically here (not dependent on OS console codepage,
    which pytest's captured stdout wouldn't hit the same way a real terminal
    does — this is exactly why the live boot in the verification pass caught
    it and the original unit test alone had not) by making the logger itself
    raise, and asserting the route still degrades cleanly.
    """
    from app import database
    from app.admin.web.routers import health

    class _BrokenSession:
        async def __aenter__(self):
            raise ConnectionError("db unreachable")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(database, "async_session_factory", lambda: _BrokenSession())

    def _raises(*a, **kw):
        raise UnicodeEncodeError("charmap", "x", 0, 1, "simulated console failure")

    monkeypatch.setattr(health.logger, "warning", _raises)

    r = await client.get("/healthz", follow_redirects=False)
    assert r.status_code == 503, "a logging failure turned a clean 503 into a crash"
    assert r.json() == {"status": "degraded"}


async def test_healthz_checks_the_real_engine_not_a_request_override(client, seeded):
    """/healthz deliberately does NOT go through Depends(get_session) — it
    calls app.database.async_session_factory directly, the same module-level
    engine every other request uses, so a health check reflects the real
    backing connection rather than whatever a request handler's DI happens to
    be pointed at. (Here that module factory is bound to the per-run test
    database; the assertion is that the route went through IT.)"""
    r = await client.get("/healthz")
    assert r.status_code == 200, "the route could not reach the DB engine"
    assert client.module_factory_calls, (
        "/healthz answered without using app.database.async_session_factory")


async def test_audit_filter_dropdown_offers_every_action_not_just_used_ones(
    auth_client, seeded
):
    """AUDIT C-26. The dropdown was built by `SELECT DISTINCT action` — a full
    scan per render that could only ever offer actions that had ALREADY
    happened. The seed writes exactly one action ("grant"), so the old code
    would render a one-item dropdown; the static list renders all of them.
    """
    from app.models.admin_log import ADMIN_LOG_ACTIONS

    body = (await auth_client.get("/audit")).text
    for action in ADMIN_LOG_ACTIONS:
        assert f'value="{action}"' in body, f"{action} missing from the filter dropdown"


async def test_audit_filter_still_narrows_results(auth_client, seeded):
    """The dropdown being static must not change what filtering DOES — the
    WHERE clause is still a real query against the column."""
    unfiltered = await auth_client.get("/audit")
    assert unfiltered.status_code == 200
    # "grant" is what the seed writes; "setprice" is a valid action with no rows.
    hit = await auth_client.get("/audit?action=grant")
    miss = await auth_client.get("/audit?action=setprice")
    assert hit.status_code == 200 and miss.status_code == 200
    assert len(miss.text) < len(hit.text), "filtering by an unused action showed rows"


# ── 4. schema exposure ───────────────────────────────────────────────────────

async def test_the_openapi_schema_is_not_served(client):
    """docs_url=None only hid the Swagger PAGE. /openapi.json stayed public and
    handed out the full route map — including every mutating path — to an
    unauthenticated caller."""
    for path in ("/openapi.json", "/docs", "/redoc"):
        r = await client.get(path, follow_redirects=False)
        assert r.status_code == 404, f"{path} is still served ({r.status_code})"


# ── 5. security headers / CSP ────────────────────────────────────────────────

async def test_security_headers_on_an_authenticated_page(auth_client, seeded):
    r = await auth_client.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in r.headers


async def test_security_headers_on_every_response_kind(client, auth_client, seeded):
    """Headers must not depend on the happy path: a redirect, a 403, a 400, a
    static file and a 404 all reach a browser too."""
    cases = [
        ("login redirect", await client.get("/users", follow_redirects=False)),
        ("login page", await client.get("/login")),
        ("static css", await auth_client.get("/static/css/app.css")),
        ("csrf 403", await auth_client.post(f"/users/{TG_PAID}/revoke",
                                            follow_redirects=False)),
        ("bad days 400", await auth_client.get("/costs?days=0")),
        ("not found", await auth_client.get("/no-such-page")),
        ("healthz", await client.get("/healthz")),
    ]
    for label, r in cases:
        assert r.headers.get("X-Frame-Options") == "DENY", label
        assert r.headers.get("X-Content-Type-Options") == "nosniff", label
        assert "Content-Security-Policy" in r.headers, label


async def test_csp_forbids_inline_script_and_external_origins(auth_client, seeded):
    """The CSP is only worth having if it is actually restrictive. Pin the
    decisions rather than the string: no 'unsafe-inline' anywhere, nothing
    loadable off-origin, and the page cannot be framed."""
    csp = (await auth_client.get("/")).headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'none'" in csp
    # data: is needed for Bootstrap's embedded form-control SVGs, and is scoped
    # to images only — never to scripts.
    assert "img-src 'self' data:" in csp
    assert "script-src 'self' 'nonce-" in csp


async def test_the_chart_script_carries_a_nonce_matching_the_header(auth_client, seeded):
    """THE test that stops the CSP silently killing the costs chart.

    Asserting the header alone would pass while the chart was blocked in every
    real browser. This ties the rendered <script> to the policy that admits it.
    """
    r = await auth_client.get("/costs")
    assert r.status_code == 200
    header_nonce = re.search(r"script-src 'self' 'nonce-([^']+)'",
                             r.headers["Content-Security-Policy"])
    assert header_nonce, "no script nonce in the CSP header"
    tag_nonce = re.search(r'<script nonce="([^"]+)">', r.text)
    assert tag_nonce, "the inline chart script rendered without a nonce"
    assert tag_nonce.group(1) == header_nonce.group(1)


async def test_the_nonce_is_fresh_on_every_response(auth_client, seeded):
    """A reused nonce is barely better than 'unsafe-inline' — an injected
    script could carry last response's value."""
    first = (await auth_client.get("/costs")).headers["Content-Security-Policy"]
    second = (await auth_client.get("/costs")).headers["Content-Security-Policy"]
    assert first != second


async def test_no_template_relies_on_an_inline_style_attribute(auth_client, seeded):
    """style-src is 'self' with no 'unsafe-inline', so a style="" attribute
    anywhere is a rule the browser drops on the floor — silently, which is the
    whole problem with CSP regressions."""
    offenders = []
    for path in ["/", "/users", "/costs", "/books", "/books/library",
                 "/revenue", "/messaging", "/alerts", "/audit",
                 f"/users/{TG_PAID}", "/login"]:
        body = (await auth_client.get(path)).text
        if re.search(r"<[^>]+\sstyle=", body):
            offenders.append(path)
    assert not offenders, f"inline style attributes CSP will drop: {offenders}"


def _form_tag(html: str, action: str) -> str:
    """The opening <form ...> tag that posts to `action` (attributes may wrap)."""
    m = re.search(r'<form\b[^>]*\baction="' + re.escape(action) + r'"[^>]*>', html)
    assert m, f"no form posting to {action}"
    return m.group(0)


async def test_destructive_forms_confirm_through_panel_js_not_an_inline_handler(
    auth_client, seeded
):
    """script-src has no 'unsafe-inline' and no 'unsafe-hashes', so an inline
    onsubmit="return confirm(...)" is never executed: the browser drops it and
    the form posts straight through. Book delete and access revoke were both
    firing with NO confirmation dialog. The prompt now lives in data-confirm and
    is enforced by /static/js/panel.js, which script-src 'self' admits."""
    cases = [
        ("/books/library", f"/books/library/{seeded['public_book'].id}/delete"),
        (f"/users/{TG_PAID}", f"/users/{TG_PAID}/revoke"),
    ]
    for page, action in cases:
        r = await auth_client.get(page)
        assert r.status_code == 200, page
        tag = _form_tag(r.text, action)
        assert "onsubmit" not in tag, f"{page}: inline handler CSP will block: {tag}"
        assert re.search(r'\bdata-confirm="[^"]+"', tag), f"{page}: no data-confirm: {tag}"
        assert '<script src="/static/js/panel.js"></script>' in r.text, (
            f"{page}: data-confirm does nothing unless panel.js is loaded")


async def test_panel_js_is_served_and_enforces_data_confirm(auth_client, seeded):
    r = await auth_client.get("/static/js/panel.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "data-confirm" in r.text and "preventDefault" in r.text


async def test_a_quote_in_a_book_title_cannot_break_out_of_the_confirm_prompt(
    auth_client, seeded, session
):
    """The old handler spliced the title into a JS string literal, so a title
    with an apostrophe produced broken (or, under a looser CSP, injectable)
    script. As an attribute value it is just autoescaped text."""
    seeded["public_book"].title = "O'zbek tili \"7\" <b>"
    await session.flush()
    r = await auth_client.get("/books/library")
    tag = _form_tag(r.text, f"/books/library/{seeded['public_book'].id}/delete")
    # Inside data-confirm specifically: the old onsubmit attribute carried the
    # same escaped bytes, which the browser un-escapes straight back into a JS
    # string literal before running it.
    assert re.search(r'\bdata-confirm="[^"]*O&#39;zbek tili &#34;7&#34; &lt;b&gt;[^"]*"',
                     tag), tag


def test_no_admin_template_carries_an_inline_event_handler():
    """Structural guard over the template SOURCES, not rendered pages, so a
    branch no fixture renders (a blocked user, an empty list) is still checked.
    Any on*="..." attribute is dead under this CSP — and dead silently, which is
    how two destructive forms lost their confirmation without anyone noticing."""
    from pathlib import Path

    templates = Path(security.__file__).resolve().parent / "templates"  # app/admin/web/templates
    sources = sorted(templates.glob("*.html"))
    assert len(sources) > 5, f"found only {len(sources)} templates in {templates}"
    offenders = []
    for path in sources:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"<[^>]*\son[a-z]+\s*=", line) or re.match(r"\s+on[a-z]+\s*=", line):
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"inline event handlers CSP will block: {offenders}"


async def test_user_rows_and_the_audit_filter_use_data_attributes(auth_client, seeded):
    users = (await auth_client.get("/users")).text
    assert f'data-href="/users/{TG_PAID}"' in users, "user row lost its click target"
    audit = (await auth_client.get("/audit")).text
    assert re.search(r'<select\b[^>]*\bname="action"[^>]*\bdata-autosubmit\b', audit), (
        "audit filter no longer submits on change")
    js = (await auth_client.get("/static/js/panel.js")).text
    assert "data-href" in js and "data-autosubmit" in js


async def test_pages_reference_no_off_origin_resource(auth_client, seeded):
    """default-src 'self' means anything off-origin is blocked. Vendored
    Bootstrap/Chart.js are served from /static; a CDN link creeping back in
    would break the page rather than merely slow it."""
    offenders = []
    for path in ["/", "/users", "/costs", "/books", "/revenue", "/messaging",
                 "/alerts", "/audit", "/login"]:
        for url in re.findall(r'(?:src|href)="([^"]+)"',
                              (await auth_client.get(path)).text):
            if url.startswith(("http://", "https://", "//")):
                offenders.append(f"{path}: {url}")
    assert not offenders, f"off-origin resources: {offenders}"


async def test_reconciliation_rows_render_with_the_shared_severity(
    auth_client, seeded, session, monkeypatch
):
    """/alerts and /costs must agree with alerts.evaluate. /costs used to
    hard-code `delta_pct > 10`, ignoring RECON_TOLERANCE_PCT, and both pages
    printed "+0.0%" for "Google billed $7, we recorded $0"."""
    monkeypatch.setattr(settings, "ALERT_DAILY_USD", 10_000.0)
    monkeypatch.setattr(settings, "RECON_TOLERANCE_PCT", 10.0)
    monkeypatch.setattr(settings, "RECON_TOLERANCE_USD", 1.0, raising=False)
    session.add_all([
        BillingReconciliation(period_label="blind-month", google_total_usd=7.0,
                              our_computed_usd=0.0, delta_usd=7.0, delta_pct=0.0),
        BillingReconciliation(period_label="big-bill", google_total_usd=108.0,
                              our_computed_usd=100.0, delta_usd=8.0, delta_pct=8.0),
    ])
    await session.flush()

    def _row(html: str, label: str) -> str:
        m = re.search(r"<tr\b[^>]*>(?:(?!</tr>).)*" + re.escape(label) + r".*?</tr>", html, re.S)
        assert m, f"no row for {label}"
        return m.group(0)

    for page in ("/alerts", "/costs"):
        html = (await auth_client.get(page)).text
        blind = _row(html, "blind-month")
        assert "table-danger" in blind, f"{page}: $0-recorded row not marked critical: {blind}"
        assert "+0.0%" not in blind, f"{page}: critical row still reads as a 0% gap: {blind}"
        big = _row(html, "big-bill")
        assert "table-warning" in big, f"{page}: $8 gap under 10% not flagged: {big}"


async def test_csv_cells_cannot_start_a_spreadsheet_formula(auth_client, seeded, session):
    """full_name is whatever a teacher typed into Telegram. Opened in Excel or
    Sheets, a cell starting with = + - @ (or a tab / carriage return before
    one) is evaluated as a formula — =HYPERLINK phishing, or DDE on older
    Excel. Such cells are exported with a leading apostrophe, the standard
    neutraliser, so the spreadsheet shows the text instead of running it."""
    import csv as _csv
    import io as _io

    payloads = ['=HYPERLINK("http://evil.example","click")', "+1+1", "-2+3",
                "@SUM(A1:A9)", "\t=1+1", "\r=1+1"]
    leaked = []
    for payload in payloads:
        seeded["paid"].full_name = payload
        await session.flush()
        r = await auth_client.get("/export/user-costs.csv")
        rows = list(_csv.reader(_io.StringIO(r.text)))
        header, body = rows[0], rows[1:]
        mine = next(row for row in body if row[header.index("telegram_id")] == str(TG_PAID))
        cell = mine[header.index("full_name")]
        if cell != "'" + payload:
            leaked.append((payload, cell))
    assert not leaked, f"formula-leading cells exported live: {leaked}"


async def test_csv_neutraliser_leaves_ordinary_and_numeric_cells_alone(auth_client, seeded):
    """Control: only formula-leading TEXT is touched. Names, ids and the
    computed dollar figures must round-trip byte-for-byte."""
    import csv as _csv
    import io as _io

    for path in ("/export/user-costs.csv", "/export/usage.csv", "/export/audit.csv"):
        r = await auth_client.get(path)
        rows = list(_csv.reader(_io.StringIO(r.text)))
        assert not any(cell.startswith("'") for row in rows for cell in row), path
    rows = list(_csv.reader(_io.StringIO((await auth_client.get("/export/user-costs.csv")).text)))
    header, body = rows[0], rows[1:]
    mine = next(row for row in body if row[header.index("telegram_id")] == str(TG_PAID))
    assert mine[header.index("full_name")] == "Paid Teacher"


# ── 6. bounded ?days= ────────────────────────────────────────────────────────

DAYS_ENDPOINTS = ["/costs", "/export/usage.csv", "/export/user-costs.csv"]


async def test_out_of_range_days_is_400_not_500_and_not_silently_defaulted(
    auth_client, seeded
):
    bad = []
    for path in DAYS_ENDPOINTS:
        for value in ("-5", "0", "99999", "366"):
            r = await auth_client.get(f"{path}?days={value}")
            if r.status_code != 400:
                bad.append(f"{path}?days={value} -> {r.status_code}")
    assert not bad, "days values that were not rejected:\n" + "\n".join(bad)


async def test_days_at_the_boundaries_is_accepted(auth_client, seeded):
    for path in DAYS_ENDPOINTS:
        for value in ("1", "365"):
            r = await auth_client.get(f"{path}?days={value}")
            assert r.status_code == 200, f"{path}?days={value} -> {r.status_code}"


async def test_a_non_numeric_days_is_rejected_without_a_500(auth_client, seeded):
    r = await auth_client.get("/costs?days=abc")
    assert 400 <= r.status_code < 500


async def test_costs_page_honours_days_instead_of_ignoring_it(auth_client, seeded):
    """It used to hardcode 30 and drop ?days= on the floor, so the heading and
    the figures disagreed with any window the admin asked for."""
    r = await auth_client.get("/costs?days=7")
    assert r.status_code == 200
    assert "7 kun" in r.text


async def test_costs_export_links_carry_the_window_being_viewed(auth_client, seeded):
    """The template used to hardcode days=30 in its CSV links independently of
    the page's own window — two constants free to drift apart."""
    body = (await auth_client.get("/costs?days=90")).text
    export_links = re.findall(r'href="(/export/[^"]+)"', body)
    assert export_links, "the CSV export links vanished from the costs page"
    for link in export_links:
        assert "days=90" in link, f"export link ignores the viewed window: {link}"


# ── 8. revenue: the payment ledger at the HTTP boundary ──────────────────────
#
# The same reason this file exists at all: the dashboard read a renamed field
# and raised on every load for seven days because nothing issued an HTTP
# request at the panel. A money page deserves at least that.

from app.models.payment import Payment  # noqa: E402
from app.services.plans import PRO, STANDART  # noqa: E402


async def _post(auth_client, path: str, page: str, **body):
    """POST with a CSRF token freshly scraped from `page`."""
    html = (await auth_client.get(page)).text
    token = _csrf_from(html)
    if path.endswith('/message') or path == '/messaging/broadcast':
        match = re.search(r'name="send_intent"\s+value="([^"]+)"', html)
        assert match, 'send confirmation intent missing'
        body.update(send_intent=match.group(1), confirmed='1')
    return await auth_client.post(path, data={**body, "csrf_token": token},
                                  follow_redirects=False)


async def test_revenue_page_renders_with_no_payments_at_all(auth_client, seeded):
    """The state the panel ships in: the ledger is empty on day one, and an
    empty page must render rather than divide by zero on a margin."""
    r = await auth_client.get("/revenue")
    assert r.status_code == 200
    assert "Tushum" in r.text
    # Both tiers listed even though neither has ever sold.
    assert "Standart" in r.text and "Pro" in r.text


async def test_revenue_nav_link_is_live_not_soon(auth_client, seeded):
    """base.html carried a disabled '💰 Revenue … soon' badge. If the slot is
    still disabled the page is unreachable from the UI no matter what the
    router does."""
    body = (await auth_client.get("/")).text
    assert '<a class="nav-link text-white ' in body
    assert re.search(r'<a\b[^>]*href="/revenue"', body), "revenue is still a dead nav slot"


async def test_recording_a_payment_through_the_panel_shows_up_everywhere(
    auth_client, seeded, session
):
    """One POST, and the number must appear in all three places that claim to
    report it: the ledger row, the user's page, and the revenue overview."""
    r = await _post(auth_client, f"/users/{TG_PAID}/payment", f"/users/{TG_PAID}",
                    plan="pro", amount_som="150000", apply_plan="1")
    assert r.status_code == 303 and r.headers["location"] == f"/users/{TG_PAID}"

    rows = (await session.execute(select(Payment))).scalars().all()
    assert len(rows) == 1 and rows[0].amount_som == 150_000

    detail = (await auth_client.get(f"/users/{TG_PAID}")).text
    assert "150 000" in detail, "the payment is not in the user's history"

    overview = (await auth_client.get("/revenue")).text
    assert "150 000" in overview, "the payment is not in the revenue overview"


async def test_the_panel_payment_applies_the_plan_through_the_shared_service(
    auth_client, seeded, session
):
    """The access half must go through admin_users.set_plan — the same function
    the bot's /plan uses — so a teacher paid through the panel gets exactly the
    bundle a teacher paid through the bot gets."""
    user = seeded["trial"]
    await _post(auth_client, f"/users/{TG_TRIAL}/payment", f"/users/{TG_TRIAL}",
                plan="standart", amount_som="75000", apply_plan="1")
    await session.refresh(user)
    assert user.monthly_variant_limit == STANDART.test_limit
    assert user.monthly_check_limit == STANDART.check_limit
    assert user.book_limit == STANDART.book_limit
    assert user.price_som_at_purchase == STANDART.price_som
    assert user.uses_left is None, "the trial meter was left in place on a paid user"


async def test_unticking_apply_plan_records_money_without_granting_access(
    auth_client, seeded, session
):
    """An unchecked HTML checkbox sends NO field at all — the route must read
    that as false rather than as a missing required field."""
    user = seeded["trial"]
    before = user.monthly_variant_limit
    r = await _post(auth_client, f"/users/{TG_TRIAL}/payment", f"/users/{TG_TRIAL}",
                    plan="standart", amount_som="75000")  # no apply_plan key
    assert r.status_code == 303
    await session.refresh(user)
    assert user.monthly_variant_limit == before, "a history-only entry granted a plan"
    assert (await session.execute(select(func.count()).select_from(Payment))).scalar_one() == 1


async def test_a_bad_date_is_reported_not_a_500(auth_client, seeded, session):
    """Operator typo, not a server fault. It must come back through the same
    flash channel as any other rejected input and write nothing."""
    r = await _post(auth_client, f"/users/{TG_PAID}/payment", f"/users/{TG_PAID}",
                    plan="pro", amount_som="150000", paid_at="16-09-2026")
    assert r.status_code == 303
    assert (await session.execute(select(func.count()).select_from(Payment))).scalar_one() == 0
    # ...and the operator is TOLD, through the flash the redirect target pops.
    followed = (await auth_client.get(f"/users/{TG_PAID}")).text
    assert "Sana noto" in followed, "the bad date was swallowed silently"


async def test_an_unknown_tariff_is_rejected_without_writing(auth_client, seeded, session):
    r = await _post(auth_client, f"/users/{TG_PAID}/payment", f"/users/{TG_PAID}",
                    plan="platinum", amount_som="999000")
    assert r.status_code == 303, "a bad plan key produced something other than a redirect"
    assert (await session.execute(select(func.count()).select_from(Payment))).scalar_one() == 0


async def test_a_back_dated_payment_keeps_both_dates(auth_client, seeded, session):
    """paid_at is when the money arrived; created_at is when it was typed in.
    A bank transfer logged a week late must not claim to have arrived today."""
    await _post(auth_client, f"/users/{TG_PAID}/payment", f"/users/{TG_PAID}",
                plan="pro", amount_som="150000", paid_at="2026-08-01")
    p = (await session.execute(select(Payment))).scalars().one()
    assert p.paid_at.strftime("%Y-%m-%d") == "2026-08-01"
    assert p.created_at > p.paid_at, "created_at was overwritten with the back-date"


async def test_the_user_page_shows_profit_beside_the_existing_cost_breakdown(
    auth_client, seeded
):
    """Task framing: profit sits ALONGSIDE the cost table already on the page,
    not instead of it. Both must still be there."""
    body = (await auth_client.get(f"/users/{TG_PAID}")).text
    assert "Xarajat (30 kun)" in body or "Xarajat" in body, "the cost breakdown vanished"
    assert "To‘lovlar / foyda" in body
    assert "Foyda" in body
    assert "revenue bosqichida" not in body, "the placeholder is still there"


# ── the amount_som number-input step/min bug ──────────────────────────────────
#
# min="1" step="1000" REJECTED the real tariff price 75000 in the browser,
# with Chrome reporting "the two nearest valid values are 74001 and 75001" —
# because HTML5's own rule for a number input is
#     valid  <=>  (value - min) is a non-negative multiple of step
# and 75000 is not 1 + 1000*n for any integer n. None of the httpx POST tests
# above caught this: they submit `data={"amount_som": "150000"}` directly,
# which is exactly what a real browser does AFTER passing its own client-side
# check — so a payload that a real user's browser would have silently blocked
# from ever being submitted was still exercised end-to-end here. This block
# closes that hole by implementing the same validity rule and applying it to
# the rendered attribute, not to the reported numbers alone.

def _input_tag(html: str, name: str) -> str:
    """The <input name="..."> tag, wherever it is in the page (attributes may
    appear in any order, so this does not assume min/step/type positions)."""
    m = re.search(r'<input\b[^>]*\bname="' + re.escape(name) + r'"[^>]*>', html)
    assert m, f"no <input name=\"{name}\"> in the page"
    return m.group(0)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(name + r'="([^"]*)"', tag)
    return m.group(1) if m else None


def _number_input_accepts(tag: str, value: float) -> bool:
    """Reimplements the browser's own number-input validity rule (the
    'stepMismatch' constraint from the HTML spec): valid iff (value - min) is
    a non-negative multiple of step. No min -> anchored at 0. No step ->
    defaults to 1. This is deliberately independent of any specific numbers
    the bug happened to be reported against."""
    min_attr = _attr(tag, "min")
    step_attr = _attr(tag, "step")
    lo = float(min_attr) if min_attr not in (None, "") else 0.0
    step = float(step_attr) if step_attr not in (None, "any", "") else 1.0
    if value < lo:
        return False
    # Float remainder with a tolerance — the same slack real browsers apply,
    # since 0.1-style steps cannot be represented exactly in binary floats.
    remainder = (value - lo) % step
    return remainder < 1e-9 or (step - remainder) < 1e-9


async def test_amount_som_input_accepts_both_real_tariff_prices(auth_client, seeded):
    """THE regression test for the reported bug. Both tiers' list prices must
    be enterable without the browser's own validation blocking the submit —
    this is a client-side check httpx never exercises, which is why the
    original min="1" step="1000" combination shipped despite every server-side
    payment test passing."""
    tag = _input_tag((await auth_client.get(f"/users/{TG_PAID}")).text, "amount_som")
    assert _attr(tag, "type") == "number"
    for price, label in ((STANDART.price_som, "Standart"), (PRO.price_som, "Pro")):
        assert _number_input_accepts(tag, price), (
            f"{label}'s price {price} is rejected by min={_attr(tag, 'min')!r} "
            f"step={_attr(tag, 'step')!r} — {tag}")


async def test_amount_som_input_accepts_arbitrary_discounted_amounts(auth_client, seeded):
    """Not just the two list prices: a negotiated or partial payment can be ANY
    positive so'm amount. Proves the fix is a general min/step rule, not a
    special-case that happens to admit 75000 and 150000 and nothing else —
    the task explicitly called out that failure mode."""
    tag = _input_tag((await auth_client.get(f"/users/{TG_PAID}")).text, "amount_som")
    for value in (1, 999, 60_000, 74_500, 100_000, 149_999, 1_234_567):
        assert _number_input_accepts(tag, value), (
            f"{value} is rejected by min={_attr(tag, 'min')!r} "
            f"step={_attr(tag, 'step')!r} — a real discounted/negotiated "
            f"payment of this amount could not be entered")


async def test_amount_som_input_still_rejects_zero_and_negative(auth_client, seeded):
    """The fix must not have widened the field into accepting nonsense —
    record_payment() itself rejects amount_som <= 0 (ValueError), and the
    client-side min should refuse it before the request is even sent."""
    tag = _input_tag((await auth_client.get(f"/users/{TG_PAID}")).text, "amount_som")
    assert not _number_input_accepts(tag, 0)
    assert not _number_input_accepts(tag, -75_000)


async def test_no_number_input_in_the_admin_panel_has_a_min_step_mismatch(auth_client, seeded):
    """Structural sweep, not just the one reported field. Same class of bug —
    min + step where min is not itself aligned to step — could have been
    copy-pasted onto any other numeric input (uses, book limit, balance,
    reconciliation). For every <input type="number"> across every page this
    session can reach, the field's OWN min must be a value the field itself
    would accept (min is always exactly at the anchor, so this is really
    'min and step do not silently exclude min itself' — the same arithmetic
    that excluded 75000 would just as easily exclude a hand-typed min)."""
    offenders = []
    for path in ["/", "/users", f"/users/{TG_PAID}", "/costs", "/books",
                 "/books/library", "/revenue", "/messaging"]:
        body = (await auth_client.get(path)).text
        for m in re.finditer(r'<input\b[^>]*\btype="number"[^>]*>', body):
            tag = m.group(0)
            min_attr, step_attr = _attr(tag, "min"), _attr(tag, "step")
            if min_attr is None or step_attr is None:
                continue  # no combination, so no possible mismatch
            if not _number_input_accepts(tag, float(min_attr)):
                name = _attr(tag, "name") or "?"
                offenders.append(f"{path} name={name}: min={min_attr!r} step={step_attr!r}")
    assert not offenders, (
        "min/step combination excludes the input's own min "
        "(the exact shape of the amount_som bug):\n" + "\n".join(offenders))


async def test_recording_the_real_standart_price_succeeds_end_to_end(auth_client, seeded, session):
    """Belt and suspenders: submit exactly what the browser would now allow
    through, and confirm the server side (already covered above for 150000)
    accepts 75000 too — the reported value, sent the way a real form submits
    it."""
    token = _csrf_from((await auth_client.get(f"/users/{TG_PAID}")).text)
    r = await auth_client.post(f"/users/{TG_PAID}/payment",
                               data={"plan": "standart", "amount_som": "75000",
                                     "csrf_token": token},
                               follow_redirects=False)
    assert r.status_code == 303
    row = (await session.execute(select(Payment).where(
        Payment.amount_som == 75_000))).scalars().one()
    assert row.plan_key == "standart"


async def test_the_revenue_page_says_how_much_cost_is_unattributed(auth_client, seeded):
    """The seed has one NULL-user usage row. The page must own up to it rather
    than present per-user profit as the whole picture."""
    body = (await auth_client.get("/revenue")).text
    assert "biriktirilmagan" in body, "the attribution gap is not disclosed"


async def test_a_user_costing_more_than_they_pay_is_flagged(auth_client, seeded):
    """The seeded paid teacher has real usage and no payments, so they are
    underwater until one is recorded — and the page must say so."""
    body = (await auth_client.get("/revenue")).text
    assert "Zarar keltirayotganlar" in body
    assert f'href="/users/{TG_PAID}"' in body, "the flagged user is not linked from the flag"


async def test_payment_is_written_to_the_audit_log_and_filterable(auth_client, seeded):
    await _post(auth_client, f"/users/{TG_PAID}/payment", f"/users/{TG_PAID}",
                plan="pro", amount_som="150000")
    audit = (await auth_client.get("/audit?action=payment")).text
    assert "payment" in audit
    # The dropdown is a fixed list (AUDIT C-26) — a new action that is not in it
    # is unselectable, which is the same as invisible.
    assert re.search(r'<option[^>]*value="payment"', audit), (
        "payment is missing from the audit filter dropdown")


# ── 9. messaging at the HTTP boundary ────────────────────────────────────────
#
# NOTHING HERE MAY REACH TELEGRAM. Every test installs a stub through
# telegram.set_bot_for_testing, and `panel_bot` (autouse below) clears it
# afterwards — so even a test that forgets cannot leak a real send, because
# get_bot() would then need a BOT_TOKEN and raise BotUnavailable instead.

from app.admin.web import telegram as panel_telegram  # noqa: E402
from app.admin.web.services import messaging as messaging_service  # noqa: E402
from app.services import broadcast as broadcast_service  # noqa: E402


async def _confirmed_fields(auth_client, page):
    html = (await auth_client.get(page)).text
    intent = re.search(r'name="send_intent"\s+value="([^"]+)"', html)
    assert intent
    return {"csrf_token": _csrf_from(html), "send_intent": intent.group(1), "confirmed": "1"}


class _PanelStubBot:
    def __init__(self, fail_for=()):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail_for:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="blocked")
        self.sent.append((chat_id, text))
        return True


@pytest_asyncio.fixture
async def panel_bot():
    """Install a stub send-only Bot for the panel, and always remove it."""
    bot = _PanelStubBot()
    panel_telegram.set_bot_for_testing(bot)
    try:
        yield bot
    finally:
        panel_telegram.set_bot_for_testing(None)


async def test_messaging_page_renders_with_every_segment(auth_client, seeded, panel_bot):
    r = await auth_client.get("/messaging")
    assert r.status_code == 200
    for seg in messaging_service.SEGMENTS:
        assert seg.label in r.text, f"segment {seg.key} is missing from the picker"


async def test_messaging_nav_link_is_live_not_soon(auth_client, seeded):
    body = (await auth_client.get("/")).text
    assert re.search(r'<a\b[^>]*href="/messaging"', body), (
        "messaging is still a dead 'soon' nav slot")


async def test_broadcast_to_a_segment_sends_through_the_bot(auth_client, seeded, panel_bot):
    """TG_PAID is the one seeded user on a live paid plan."""
    send_fields = await _confirmed_fields(auth_client, "/messaging")
    r = await auth_client.post("/messaging/broadcast",
                               data={"segment": "paid_active", "text": "Yangilik bor",
                                     **send_fields},
                               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/messaging"
    assert [c for c, _ in panel_bot.sent] == [TG_PAID]
    assert panel_bot.sent[0][1].startswith(broadcast_service.PREFIX)
    assert "Yangilik bor" in panel_bot.sent[0][1]


async def test_an_empty_broadcast_body_is_refused_and_sends_nothing(
    auth_client, seeded, panel_bot
):
    send_fields = await _confirmed_fields(auth_client, "/messaging")
    r = await auth_client.post("/messaging/broadcast",
                               data={"segment": "paid_active", "text": "   ",
                                     **send_fields},
                               follow_redirects=False)
    assert r.status_code == 303
    assert panel_bot.sent == [], "an empty announcement was broadcast"


async def test_an_over_long_broadcast_is_refused_before_telegram_rejects_it(
    auth_client, seeded, panel_bot
):
    """broadcast.py prefixes every message, so a body at Telegram's own 4096
    ceiling produces a 400 that would be miscounted as 'the user blocked us'."""
    send_fields = await _confirmed_fields(auth_client, "/messaging")
    r = await auth_client.post(
        "/messaging/broadcast",
        data={"segment": "paid_active", "text": "x" * 5000, **send_fields},
        follow_redirects=False)
    assert r.status_code == 303
    assert panel_bot.sent == []
    assert "juda uzun" in (await auth_client.get("/messaging")).text


async def test_an_unknown_segment_is_refused_and_does_not_message_everyone(
    auth_client, seeded, panel_bot
):
    """The failure mode that matters: a bad key must not fall through to 'all'."""
    send_fields = await _confirmed_fields(auth_client, "/messaging")
    r = await auth_client.post("/messaging/broadcast",
                               data={"segment": "everyone", "text": "Salom",
                                     **send_fields},
                               follow_redirects=False)
    assert r.status_code == 303
    assert panel_bot.sent == [], "an unknown segment broadcast to somebody"


async def test_sending_one_user_a_message_from_their_detail_page(
    auth_client, seeded, panel_bot
):
    send_fields = await _confirmed_fields(auth_client, f"/users/{TG_PAID}")
    r = await auth_client.post(f"/users/{TG_PAID}/message",
                               data={"text": "Shaxsiy xabar", **send_fields},
                               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/users/{TG_PAID}"
    assert [c for c, _ in panel_bot.sent] == [TG_PAID]
    # The DM prefix, not the broadcast one — same as the bot's /message.
    assert panel_bot.sent[0][1].startswith(broadcast_service.DM_PREFIX)


async def test_a_blocked_recipient_is_reported_not_a_500(auth_client, seeded):
    bot = _PanelStubBot(fail_for={TG_PAID})
    panel_telegram.set_bot_for_testing(bot)
    try:
        send_fields = await _confirmed_fields(auth_client, f"/users/{TG_PAID}")
        r = await auth_client.post(f"/users/{TG_PAID}/message",
                                   data={"text": "Salom", **send_fields},
                                   follow_redirects=False)
        assert r.status_code == 303
        assert bot.sent == []
        assert "Yetkazib bo" in (await auth_client.get(f"/users/{TG_PAID}")).text
    finally:
        panel_telegram.set_bot_for_testing(None)


async def test_every_send_is_audited(auth_client, seeded, panel_bot, session, monkeypatch):
    from app.admin.web import send_guard
    now = [1000.0]
    monkeypatch.setattr(send_guard, "_clock", lambda: now[0])
    send_fields = await _confirmed_fields(auth_client, "/messaging")
    await auth_client.post("/messaging/broadcast",
                           data={"segment": "paid_active", "text": "Salom",
                                 **send_fields},
                           follow_redirects=False)
    now[0] += send_guard.ACTION_INTERVAL_SECONDS
    send_fields = await _confirmed_fields(auth_client, f"/users/{TG_TRIAL}")
    await auth_client.post(f"/users/{TG_TRIAL}/message",
                           data={"text": "Salom", **send_fields},
                           follow_redirects=False)

    actions = {a for (a,) in (await session.execute(
        select(AdminLog.action).where(AdminLog.action.in_(("broadcast", "message"))))).all()}
    assert actions == {"broadcast", "message"}


async def test_the_panel_sends_nothing_when_no_bot_token_is_configured(
    auth_client, seeded, monkeypatch
):
    """The panel builds its own Bot (it is a separate process from the bot and
    gets none injected). With no token it must say so, not raise."""
    panel_telegram.set_bot_for_testing(None)
    monkeypatch.setattr(settings, "BOT_TOKEN", "")
    send_fields = await _confirmed_fields(auth_client, "/messaging")
    r = await auth_client.post("/messaging/broadcast",
                               data={"segment": "paid_active", "text": "Salom",
                                     **send_fields},
                               follow_redirects=False)
    assert r.status_code == 303
    assert "BOT_TOKEN" in (await auth_client.get("/messaging")).text


async def test_the_broadcast_form_confirms_before_sending(auth_client, seeded, panel_bot):
    """A broadcast has no undo. Under this CSP the prompt must be data-confirm
    (an inline onsubmit is silently dropped — that is how two destructive forms
    lost their confirmation before)."""
    body = (await auth_client.get("/messaging")).text
    tag = _form_tag(body, "/messaging/broadcast")
    assert "onsubmit" not in tag
    assert re.search(r'\bdata-confirm="[^"]+"', tag), tag
    assert '<script src="/static/js/panel.js"></script>' in body

    detail = (await auth_client.get(f"/users/{TG_PAID}")).text
    dm_tag = _form_tag(detail, f"/users/{TG_PAID}/message")
    assert re.search(r'\bdata-confirm="[^"]+"', dm_tag), dm_tag


async def test_the_messaging_page_states_what_is_deliberately_not_stored(
    auth_client, seeded, panel_bot
):
    """History, templates and delivery status are descoped. The page says so,
    so nobody goes looking for a feature that was decided against."""
    body = (await auth_client.get("/messaging")).text
    assert "saqlanmaydi" in body
