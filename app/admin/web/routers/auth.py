"""Login / logout for the admin panel."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..rate_limit import is_locked_out, record_failure, record_success
from ..security import (
    check_credentials,
    is_logged_in,
    login_enabled,
    logout,
    mark_logged_in,
    require_csrf,
    require_login,
)
from ..templating import templates

router = APIRouter()


def _safe_next(nxt: str | None) -> str:
    """Only allow same-site relative redirects (no //host, no scheme) so ?next=
    can't be used as an open redirect."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> HTMLResponse:
    if is_logged_in(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"enabled": login_enabled(), "next": _safe_next(next), "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
) -> HTMLResponse:
    locked, retry_after = is_locked_out(request)
    if locked:
        minutes = max(1, (retry_after + 59) // 60)
        return templates.TemplateResponse(
            request, "login.html",
            {"enabled": login_enabled(), "next": _safe_next(next),
             "error": f"Juda ko‘p noto‘g‘ri urinish. {minutes} daqiqadan keyin qayta urining."},
            status_code=429,
        )

    if check_credentials(username, password):
        record_success(request)
        mark_logged_in(request)
        return RedirectResponse(_safe_next(next), status_code=303)

    record_failure(request)
    return templates.TemplateResponse(
        request, "login.html",
        {"enabled": login_enabled(), "next": _safe_next(next),
         "error": "Login yoki parol noto‘g‘ri." if login_enabled()
                  else "Panel paroli o‘rnatilmagan (ADMIN_PASSWORD_HASH)."},
        status_code=401,
    )


@router.post("/logout", dependencies=[Depends(require_login), Depends(require_csrf)])
async def logout_submit(request: Request) -> RedirectResponse:
    logout(request)
    return RedirectResponse("/login", status_code=303)
