"""
Entry point for the HTML admin panel (separate from the bot).

Run:
    python admin_main.py                 # serves on 0.0.0.0:8001
    uvicorn app.admin.web.app:app --port 8001 --host 0.0.0.0

Binds 0.0.0.0 so it is reachable from any device on the network (phone, laptop);
put it behind HTTPS + a reverse proxy for public/VPS use.
"""
from __future__ import annotations

import os

import uvicorn

from app.config import settings

if __name__ == "__main__":
    # AUDIT B-10. uvicorn defaults to proxy_headers=True with
    # forwarded_allow_ips="127.0.0.1", so behind the reverse proxy this file's
    # own docstring recommends — same host, therefore peer 127.0.0.1 — the
    # CLIENT-SUPPLIED X-Forwarded-For became request.client.host, and rotating
    # it gave unlimited guesses against the login lockout. Trust is now
    # declared explicitly and defaults to NOTHING.
    _trusted = [
        ip.strip() for ip in settings.ADMIN_TRUSTED_PROXIES.split(",") if ip.strip()
    ]
    # AUDIT B-40. The session cookie is https_only=True, so over plain HTTP the
    # browser silently refuses to store it and the login redirects forever with
    # NO error shown — the panel looks broken for a reason nothing reports.
    # This file's own default is http://0.0.0.0:8001, so that is the DEFAULT
    # experience for a fresh deploy. Warn loudly rather than change the cookie
    # policy: dropping https_only would make an authenticated admin session
    # sniffable, which is worse than a confusing first run.
    if not _trusted:
        print(
            "[warning] The admin session cookie is https_only. Serve this "
            "panel behind HTTPS (a reverse proxy) or logins will silently "
            "fail to stick. If you DO run a proxy, set ADMIN_TRUSTED_PROXIES "
            "to its address so the login lockout sees real client IPs.",
            flush=True,
        )

    uvicorn.run(
        "app.admin.web.app:app",
        host=os.getenv("ADMIN_PANEL_HOST", "0.0.0.0"),
        port=int(os.getenv("ADMIN_PANEL_PORT", "8001")),
        reload=bool(os.getenv("ADMIN_PANEL_RELOAD")),
        proxy_headers=bool(_trusted),
        forwarded_allow_ips=_trusted or None,
    )
