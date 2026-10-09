"""
Security response headers for the admin panel, including a per-request CSP
nonce.

WHY A NONCE RATHER THAN 'unsafe-inline'. The panel renders teacher-supplied
strings — usernames, full names, book titles — straight into its pages. Jinja2
autoescapes, so that is not an open XSS today, but CSP is the layer that holds
when escaping is missed once. `script-src 'self' 'unsafe-inline'` would forfeit
exactly that, because 'unsafe-inline' permits any injected <script> too. So the
one inline block in the app (the Chart.js setup on /costs) carries a
per-response nonce instead, and everything else must come from /static.

`style-src` is 'self' with NO 'unsafe-inline': the three inline `style=`
attributes that used to exist were moved into app.css, because nonces do not
apply to style attributes (only to <style> elements), so leaving them would
have forced 'unsafe-inline' on styles for the sake of three width rules.

`img-src` needs `data:` — vendored Bootstrap embeds its radio/switch/checkbox
glyphs as `url("data:image/svg+xml,...")`, and CSS background images are
img-src. Dropping `data:` renders form controls blank, silently.

Implemented as pure ASGI rather than BaseHTTPMiddleware: BaseHTTPMiddleware
wraps every response in a stream and interacts badly with background tasks, and
this needs nothing from that machinery beyond a header append.
"""
from __future__ import annotations

import secrets

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Static, request-independent headers.
STATIC_HEADERS: dict[str, str] = {
    # Authenticated pages and exports must not survive logout in a shared cache.
    "Cache-Control": "no-store",
    # Defence in depth with frame-ancestors below: still honoured by older
    # browsers that do not implement CSP3.
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Send the full URL only to same-origin destinations; bare origin
    # cross-origin, nothing over a downgrade. Panel URLs carry telegram ids.
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

# CSP with a %s slot for the per-request nonce.
_CSP_TEMPLATE = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'nonce-%s'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
])


def csp_nonce(request) -> str:
    """The nonce for THIS response, for templates to stamp on an inline script.

    Returns "" when the middleware did not run (e.g. a template rendered
    directly in a unit test). An empty nonce renders `nonce=""`, which matches
    no CSP nonce and so fails closed — the script is blocked rather than
    silently trusted.
    """
    return getattr(request.state, "csp_nonce", "")


class SecurityHeadersMiddleware:
    """Attach the security headers to every response, static files included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nonce = secrets.token_urlsafe(16)
        # Starlette's Request.state reads through to scope["state"], so the
        # route (and the template it renders) can reach the nonce from here.
        scope.setdefault("state", {})["csp_nonce"] = nonce

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in STATIC_HEADERS.items():
                    headers.setdefault(key, value)
                headers.setdefault("Content-Security-Policy", _CSP_TEMPLATE % nonce)
            await send(message)

        await self.app(scope, receive, send_with_headers)
