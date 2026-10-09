"""The suite must never reach the real Gemini API.

There was no guard: a test that reached an unpatched Gemini path sent a real,
billed request with whatever key .env held. Mocks are per test, so one missed
patch was enough. Production code also swallows most call failures (the grading
reader returns an empty read, generation marks the theme errored), so a raised
network error alone would not fail the test either.

`block_live_gemini` (autouse, tests/conftest.py) stops the request before it
leaves the process AND fails the test at teardown when anything tried, whether
or not the app swallowed the error.
"""
from __future__ import annotations

import socket

import pytest
import requests

from tests.network_guard import (
    GEMINI_HOST,
    LiveGeminiCallBlocked,
    assert_no_live_calls,
)

URL = f"https://{GEMINI_HOST}/v1beta/models/gemini-2.5-flash:generateContent"


def test_a_requests_call_to_gemini_is_stopped_before_it_leaves(block_live_gemini):
    with pytest.raises(LiveGeminiCallBlocked):
        requests.post(URL, json={}, timeout=5)

    assert block_live_gemini, "the attempt was not recorded"
    block_live_gemini.clear()  # this test made the attempt on purpose


def test_a_dns_lookup_of_the_gemini_host_is_stopped(block_live_gemini):
    """Covers clients that bypass requests (aiohttp, httpx, urllib): every one
    of them has to resolve the host first."""
    with pytest.raises(LiveGeminiCallBlocked):
        socket.getaddrinfo(GEMINI_HOST, 443)

    assert block_live_gemini
    block_live_gemini.clear()


def test_other_hosts_are_not_touched():
    assert socket.getaddrinfo("localhost", 5432)


def test_a_swallowed_attempt_still_fails_the_test():
    """The teardown check, which is what catches an attempt the app caught."""
    with pytest.raises(pytest.fail.Exception, match=GEMINI_HOST):
        assert_no_live_calls([URL])


def test_no_attempt_passes_quietly():
    assert_no_live_calls([])
