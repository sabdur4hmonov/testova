"""Stop the test suite from reaching the real Gemini API.

Installed for every test by the autouse `block_live_gemini` fixture in
tests/conftest.py. Two layers, because a request can leave by either:

  * requests' HTTPAdapter.send, which every requests-based caller goes through
    (gemini_rest.call, sheet_reader._call_sync). This layer also catches a
    request that would have gone out through an HTTP(S) proxy, where the
    Gemini host is never resolved locally;
  * socket.getaddrinfo, which any other client (aiohttp, httpx, urllib) needs
    before it can connect.

A blocked attempt raises LiveGeminiCallBlocked AND is recorded. The record is
what makes it loud: production code swallows most call failures (an empty
grading read, an errored theme), so the fixture fails the test at teardown
whenever the record is not empty.

LiveGeminiCallBlocked is deliberately not a requests.ConnectionError, so
gemini_rest's transport retry does not sleep through three attempts first.
"""
from __future__ import annotations

import socket
from urllib.parse import urlsplit

import pytest
import requests.adapters

GEMINI_HOST = "generativelanguage.googleapis.com"


class LiveGeminiCallBlocked(RuntimeError):
    """A test tried to reach the real Gemini API."""


def _is_gemini(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    return isinstance(host, str) and host.lower().rstrip(".") == GEMINI_HOST


def install(monkeypatch) -> list[str]:
    """Patch both layers for the current test. Returns the attempt record."""
    attempts: list[str] = []

    real_getaddrinfo = socket.getaddrinfo
    real_send = requests.adapters.HTTPAdapter.send

    def guarded_getaddrinfo(host, *args, **kwargs):
        if _is_gemini(host):
            attempts.append(f"dns:{host}")
            raise LiveGeminiCallBlocked(f"test tried to resolve {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_send(self, request, *args, **kwargs):
        if _is_gemini(urlsplit(request.url).hostname):
            attempts.append(request.url)
            raise LiveGeminiCallBlocked(f"test tried to call {request.url}")
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", guarded_send)
    return attempts


def assert_no_live_calls(attempts: list[str]) -> None:
    if attempts:
        pytest.fail(
            "test tried to reach the real Gemini API (mock the call): "
            + ", ".join(attempts),
            pytrace=False,
        )
