"""The online-test website: <ONLINE_BASE_URL>/t/<code>.

Plain HTML + a little JS (online/web/), no framework. Every rule lives in
online/service.py; this module is HTTP only: cookies, rate limits, JSON.

API (all JSON; POSTs must carry `X-Requested-With: testova` — a custom header
a cross-site form cannot send, which together with the SameSite cookie keeps
other sites from acting for a student):

    GET  /api/t/{code}/info                 title, N, timers — no attempt needed
    POST /api/t/{code}/start   {first_name, last_name, class_name}
    GET  /api/t/{code}/state                the student's attempt
    GET  /api/t/{code}/q/{pos}              one question (no answers, no ids)
    POST /api/t/{code}/goto    {position}
    POST /api/t/{code}/answer  {position, choices:[index]}
    POST /api/t/{code}/event   {kind: "hidden"|"visible"}
    POST /api/t/{code}/finish
    GET  /api/t/{code}/img/{pos}            the question's picture, own attempt only
"""
from __future__ import annotations

import ipaddress
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.models.online_test import ATTEMPT_ACTIVE, ATTEMPT_ANNULLED, TEST_OPEN
from app.utils.logging import get_logger
from online import service as svc
from online.ratelimit import Limits

logger = get_logger(__name__)

WEB = Path(__file__).resolve().parent / "web"
COOKIE_MAX_AGE = 24 * 3600
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "testova"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class StartBody(BaseModel):
    first_name: str = Field(..., max_length=100)
    last_name: str = Field(..., max_length=100)
    class_name: str = Field(..., max_length=50)


class GotoBody(BaseModel):
    position: int


class AnswerBody(BaseModel):
    position: int
    choices: list[int] = Field(default_factory=list, max_length=12)


class EventBody(BaseModel):
    kind: str = Field(..., max_length=16)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _trusted(peer: str, proxies: str) -> bool:
    nets = [p.strip() for p in proxies.split(",") if p.strip()]
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for n in nets:
        try:
            if ip in ipaddress.ip_network(n, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd and _trusted(peer, settings.ONLINE_TRUSTED_PROXIES):
        return fwd.split(",")[-1].strip() or peer
    return peer


def _image_file(image_path: str) -> Path | None:
    """The question picture on disk, only from the two stores the bot writes."""
    roots = [Path("temp_images").resolve(), Path("storage").resolve()]
    candidates = [Path(image_path)]
    try:
        from app.services import storage
        candidates.append(storage.get_local_path(image_path))
    except Exception:  # noqa: BLE001 — s3 mode or a bad key: just no picture
        pass
    for c in candidates:
        try:
            p = c.resolve()
        except OSError:
            continue
        if p.is_file() and any(p.is_relative_to(r) for r in roots):
            return p
    return None


def create_app(session_factory=None, clock: Callable[[], datetime] = _utcnow,
               limits: Limits | None = None) -> FastAPI:
    if session_factory is None:
        from app.database import async_session_factory as session_factory
    limits = limits or Limits()
    app = FastAPI(title="Testova online", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def err(http_status: int, code: str, info: dict | None = None) -> JSONResponse:
        return JSONResponse({"error": code, **(info or {})}, status_code=http_status)

    def cookie_name(code: str) -> str:
        return f"ot_{code}"

    async def load(request: Request, session, code: str, need_attempt: bool = True,
                   judge_away: bool = True):
        """(test, attempt). Unknown codes count against the guessing limit."""
        if not settings.ONLINE_TEST_ENABLED:
            raise HTTPException(503, "disabled")
        ip = client_ip(request)
        if limits.code_lookup_blocked(ip):
            raise HTTPException(429, "too_many")
        test = await svc.test_by_code(session, code)
        if test is None:
            limits.bad_code(ip)
            raise HTTPException(404, "no_such_test")
        token = request.cookies.get(cookie_name(test.code))
        attempt = await svc.attempt_by_token(session, test, token, lock=need_attempt)
        if need_attempt:
            if attempt is None:
                raise HTTPException(401, "no_attempt")
            if limits.per_token.hit(attempt.token_hash):
                raise HTTPException(429, "too_many")
            await svc.refresh(session, attempt, test, clock(), judge_away=judge_away)
            # Durable now: a refusal later in the request must not undo a
            # violation or an expiry that refresh() just decided. The commit
            # releases the row lock, so take it again (and re-read the row)
            # for the rest of the request.
            await session.commit()
            attempt = await svc.attempt_by_token(session, test, token, lock=True)
        return test, attempt

    def check_csrf(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(403, "bad_request")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return err(exc.status_code, str(exc.detail))

    @app.exception_handler(svc.OnlineError)
    async def online_error(_request: Request, exc: svc.OnlineError):
        return err(409, exc.code, exc.info)

    # ── pages ───────────────────────────────────────────────────────────────
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "enabled": settings.ONLINE_TEST_ENABLED}

    @app.get("/t/{code}", response_class=HTMLResponse)
    async def page(code: str):
        return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"))

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404, "not_found")
        return FileResponse(WEB / name, media_type=mimetypes.guess_type(name)[0],
                            headers={"Cache-Control": "public, max-age=300"})

    # ── API ─────────────────────────────────────────────────────────────────
    @app.get("/api/t/{code}/info")
    async def info(code: str, request: Request):
        async with session_factory() as session:
            test, attempt = await load(request, session, code, need_attempt=False)
            has_attempt = attempt is not None and attempt.status != ATTEMPT_ANNULLED
            return {
                "title": test.title, "lang": test.lang, "open": test.status == TEST_OPEN,
                "question_count": test.question_count, "total_seconds": test.total_seconds,
                "per_question_seconds": test.per_question_seconds,
                "allow_back": test.allow_back, "has_attempt": has_attempt,
            }

    @app.post("/api/t/{code}/start")
    async def start(code: str, body: StartBody, request: Request):
        check_csrf(request)
        if limits.start_ip.hit(client_ip(request)):
            raise HTTPException(429, "too_many")
        async with session_factory() as session:
            test, attempt = await load(request, session, code, need_attempt=False)
            if attempt is not None and attempt.status != ATTEMPT_ANNULLED:
                raise svc.OnlineError("already_started")
            now = clock()
            attempt, token = await svc.start_attempt(
                session, test, first_name=body.first_name, last_name=body.last_name,
                class_name=body.class_name, now=now,
            )
            view = await svc.attempt_view(session, attempt, test, now)
            await session.commit()
        resp = JSONResponse(view)
        resp.set_cookie(
            cookie_name(test.code), token, max_age=COOKIE_MAX_AGE, httponly=True,
            samesite="strict", path="/",
            secure=settings.ONLINE_BASE_URL.startswith("https://"),
        )
        return resp

    @app.get("/api/t/{code}/state")
    async def state(code: str, request: Request):
        async with session_factory() as session:
            test, attempt = await load(request, session, code)
            view = await svc.attempt_view(session, attempt, test, clock())
            await session.commit()
            return view

    @app.get("/api/t/{code}/q/{position}")
    async def question(code: str, position: int, request: Request):
        async with session_factory() as session:
            test, attempt = await load(request, session, code)
            if attempt.status != ATTEMPT_ACTIVE:
                await session.commit()
                raise svc.OnlineError("attempt_over", status=attempt.status)
            if position != attempt.current_position:
                raise svc.OnlineError("not_current")
            aq = await svc._aq_at(session, attempt, position)
            view = await svc.question_view(session, attempt, test, aq, clock())
            await session.commit()
            return view

    @app.post("/api/t/{code}/goto")
    async def goto(code: str, body: GotoBody, request: Request):
        check_csrf(request)
        async with session_factory() as session:
            test, attempt = await load(request, session, code)
            now = clock()
            aq = await svc.goto(session, attempt, test, body.position, now)
            view = await svc.question_view(session, attempt, test, aq, now)
            await session.commit()
            return view

    @app.post("/api/t/{code}/answer")
    async def answer(code: str, body: AnswerBody, request: Request):
        check_csrf(request)
        async with session_factory() as session:
            test, attempt = await load(request, session, code)
            now = clock()
            aq = await svc.answer(session, attempt, test, body.position, body.choices, now)
            await session.commit()
            return {"saved": True, "position": aq.position}

    @app.post("/api/t/{code}/event")
    async def event(code: str, body: EventBody, request: Request):
        check_csrf(request)
        async with session_factory() as session:
            test, attempt = await load(request, session, code, judge_away=False)
            out = await svc.report(session, attempt, test, body.kind, clock())
            await session.commit()
            return {
                "counted": out.counted, "violations": out.violations,
                "remaining_chances": out.remaining_chances,
                "replaced": out.replaced_positions, "caught": out.caught,
            }

    @app.post("/api/t/{code}/finish")
    async def finish(code: str, request: Request):
        check_csrf(request)
        async with session_factory() as session:
            test, attempt = await load(request, session, code)
            now = clock()
            await svc.finish(session, attempt, now)
            view = await svc.attempt_view(session, attempt, test, now)
            await session.commit()
            return view

    @app.get("/api/t/{code}/img/{position}")
    async def image(code: str, position: int, request: Request):
        async with session_factory() as session:
            test, attempt = await load(request, session, code)
            if attempt.status != ATTEMPT_ACTIVE:
                raise svc.OnlineError("attempt_over", status=attempt.status)
            aq = await svc._aq_at(session, attempt, position)
            from app.models.question import Question
            q = await session.get(Question, aq.question_id)
            await session.commit()
        path = _image_file(q.image_path) if q and q.has_image and q.image_path else None
        if path is None:
            raise HTTPException(404, "no_image")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "image/png",
                            headers={"Cache-Control": "private, no-store"})

    @app.get("/", response_class=Response)
    async def root():
        return Response(status_code=404)

    return app


app = create_app()
