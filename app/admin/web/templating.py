"""Shared Jinja2 environment for the admin panel (imported by app + routers,
so neither has to import the other)."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .headers import csp_nonce
from .security import get_csrf_token
from .send_guard import issue as issue_send_intent

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
# Templates call csrf_token(request) inside every <form method="post"> to
# render the hidden field; `request` is already in scope (TemplateResponse
# puts it there automatically).
templates.env.globals["csrf_token"] = get_csrf_token
templates.env.globals["send_intent"] = issue_send_intent
# Templates stamp this on an inline <script> so CSP admits it without
# 'unsafe-inline'. Same call shape as csrf_token(request).
templates.env.globals["csp_nonce"] = csp_nonce
