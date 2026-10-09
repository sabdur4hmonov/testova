"""The logging chain must render exc_info as a real traceback, not a bare tuple.

The download TimeoutError logged as "TimeoutError: " with no stack because the
structlog processor chain had no exc_info formatter. This pins the fix.
"""
from __future__ import annotations

import sys

import structlog

from app.utils import logging as applog


def test_format_exc_info_is_in_the_shared_chain():
    # Guards against silent removal — this processor is the whole fix.
    assert structlog.processors.format_exc_info in applog._SHARED_PROCESSORS


def test_exc_info_is_rendered_as_a_full_traceback():
    try:
        raise TimeoutError()  # the exact bare, empty-message error from the bug
    except TimeoutError:
        event = structlog.processors.format_exc_info(
            None, "warning", {"event": "boom", "exc_info": sys.exc_info()}
        )

    # exc_info consumed, a formatted 'exception' string produced instead
    assert "exc_info" not in event
    assert "exception" in event
    # a REAL stack, not just the repr the old config emitted
    assert "Traceback" in event["exception"]
    assert "TimeoutError" in event["exception"]
    assert "test_exc_info_is_rendered_as_a_full_traceback" in event["exception"]
