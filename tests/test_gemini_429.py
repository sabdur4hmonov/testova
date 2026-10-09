"""HTTP 429: bounded exponential backoff with jitter, and per-role slots.

Before this, a 429 raised on the first attempt everywhere except grading, where
it fell into the generic flat 1-second retry and used up a read attempt. A
per-minute limit clears in seconds, so one teacher's burst failed a whole theme
or asked for a retake of a perfectly readable photo.

What these pin:
  * the wait is max(server hint, exponential backoff) plus bounded jitter,
    clamped to the role's max single wait, inside the role's total budget;
  * waiting is refused when it is pointless: a hint longer than the max single
    wait, or a per-day quota;
  * a 429 never switches model, and the 404 fallback is untouched;
  * only the attempt that succeeds writes a usage row;
  * a role slot is held around one HTTP attempt only, never during a backoff
    sleep, and is released on any exception.

Nothing here sleeps for real or reaches the network: sleeps go through the
gemini_throttle seams and requests.post is replaced in every test.
"""
from __future__ import annotations

import asyncio
import email.utils
import threading
import time

import pytest
import requests
from PIL import Image

from app.config import settings
from app.services import gemini_rest, gemini_throttle, sheet_reader
from app.services.gemini_throttle import GeminiQuotaExceeded, RetryHint

FALLBACK_MODEL = "gemini-3.1-flash-lite"
DAILY_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel"


# ── fakes ────────────────────────────────────────────────────────────────────

def _error_body(retry_delay=None, quota_id=None):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}]})
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": retry_delay})
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": details}}


class _Resp:
    def __init__(self, status=200, *, body=None, headers=None, text="ok"):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._text = text

    def json(self):
        if self._body is not None:
            return self._body
        return {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
                "candidates": [{"finishReason": "STOP",
                                "content": {"parts": [{"text": self._text}]}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


def _r429(retry_delay=None, quota_id=None, headers=None):
    return _Resp(429, body=_error_body(retry_delay, quota_id), headers=headers)


def _script(monkeypatch, responses):
    calls, queue = [], list(responses)

    def post(url, headers=None, json=None, timeout=None):
        calls.append(url.split("/models/")[1].split(":")[0])
        return queue.pop(0)

    monkeypatch.setattr(requests, "post", post)
    return calls


class _Rng:
    """Deterministic jitter: returns the low or high end of the range asked."""

    def __init__(self, high=False):
        self.high, self.asked = high, []

    def uniform(self, a, b):
        self.asked.append((a, b))
        return b if self.high else a


@pytest.fixture
def sleeps(monkeypatch):
    """Record every backoff sleep instead of sleeping; zero jitter."""
    slept = []
    monkeypatch.setattr(gemini_throttle, "_sleep", slept.append)
    monkeypatch.setattr(gemini_throttle, "_rng", _Rng(high=False))
    return slept


@pytest.fixture
def vision_policy(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_429_BACKOFF_BASE_SECONDS", 2.0)
    monkeypatch.setattr(settings, "GEMINI_429_JITTER_FRACTION", 0.25)
    monkeypatch.setattr(settings, "GEMINI_429_MAX_RETRIES_VISION", 4)
    monkeypatch.setattr(settings, "GEMINI_429_MAX_WAIT_SECONDS_VISION", 30.0)
    monkeypatch.setattr(settings, "GEMINI_429_BUDGET_SECONDS_VISION", 60.0)


def _vision_call():
    return gemini_rest.call("p", api_key="K", role="vision", kind="book_extract",
                            call_site="book.theme_vision")


# ── the suite-wide sleep guard (tests/conftest.py) ───────────────────────────

def test_an_unpatched_backoff_sleep_is_recorded_not_slept(block_real_backoff_sleep):
    started = time.monotonic()
    gemini_throttle.sleep(30.0)

    assert time.monotonic() - started < 1.0
    assert block_real_backoff_sleep == [30.0]
    block_real_backoff_sleep.clear()  # made on purpose


async def test_an_unpatched_async_backoff_sleep_is_recorded_not_slept(block_real_backoff_sleep):
    await gemini_throttle.asleep(30.0)

    assert block_real_backoff_sleep == [30.0]
    block_real_backoff_sleep.clear()


# ── reading the server's hint ────────────────────────────────────────────────

@pytest.mark.parametrize("delay,seconds", [("13s", 13.0), ("1.5s", 1.5), ("0s", 0.0)])
def test_retry_delay_in_the_error_body_is_read(delay, seconds):
    assert gemini_throttle.parse_retry_hint(_r429(delay)) == RetryHint(seconds, False)


def test_retry_after_seconds_header_is_read():
    hint = gemini_throttle.parse_retry_hint(_r429(headers={"Retry-After": "7"}))
    assert hint == RetryHint(7.0, False)


def test_retry_after_http_date_header_is_read():
    when = email.utils.formatdate(time.time() + 20, usegmt=True)
    hint = gemini_throttle.parse_retry_hint(_r429(headers={"Retry-After": when}))
    assert 17.0 <= hint.seconds <= 21.0


def test_the_larger_of_header_and_body_hint_wins():
    hint = gemini_throttle.parse_retry_hint(_r429("4s", headers={"Retry-After": "9"}))
    assert hint.seconds == 9.0


def test_a_per_day_quota_is_recognised():
    hint = gemini_throttle.parse_retry_hint(_r429("5s", quota_id=DAILY_QUOTA_ID))
    assert hint.daily is True


def test_a_429_without_any_hint_or_body_parses_to_no_hint():
    assert gemini_throttle.parse_retry_hint(_Resp(429, body={})) == RetryHint(None, False)

    class _NotJson(_Resp):
        def json(self):
            raise ValueError("not json")

    assert gemini_throttle.parse_retry_hint(_NotJson(429)) == RetryHint(None, False)


# ── the wait itself ──────────────────────────────────────────────────────────

POLICY = gemini_throttle.Policy(max_retries=4, max_wait=30.0, budget=60.0)


def test_backoff_doubles_from_the_base(monkeypatch, vision_policy):
    monkeypatch.setattr(gemini_throttle, "_rng", _Rng(high=False))
    waits = [gemini_throttle.next_wait(POLICY, RetryHint(None, False), n, 0.0)[0]
             for n in range(4)]
    assert waits == [2.0, 4.0, 8.0, 16.0]


def test_the_server_hint_wins_when_it_is_longer(monkeypatch, vision_policy):
    monkeypatch.setattr(gemini_throttle, "_rng", _Rng(high=False))
    assert gemini_throttle.next_wait(POLICY, RetryHint(13.0, False), 0, 0.0)[0] == 13.0


def test_the_backoff_wins_when_it_is_longer(monkeypatch, vision_policy):
    monkeypatch.setattr(gemini_throttle, "_rng", _Rng(high=False))
    assert gemini_throttle.next_wait(POLICY, RetryHint(1.0, False), 2, 0.0)[0] == 8.0


def test_jitter_is_added_on_top_and_stays_within_its_fraction(monkeypatch, vision_policy):
    rng = _Rng(high=True)
    monkeypatch.setattr(gemini_throttle, "_rng", rng)

    wait, _ = gemini_throttle.next_wait(POLICY, RetryHint(8.0, False), 0, 0.0)

    assert rng.asked == [(0.0, 0.25 * 8.0)], "jitter is only ever added, up to 25%"
    assert wait == 10.0


def test_the_max_single_wait_is_never_exceeded(monkeypatch, vision_policy):
    monkeypatch.setattr(gemini_throttle, "_rng", _Rng(high=True))
    policy = gemini_throttle.Policy(max_retries=10, max_wait=30.0, budget=1000.0)

    wait, _ = gemini_throttle.next_wait(policy, RetryHint(28.0, False), 6, 0.0)

    assert wait == 30.0


@pytest.mark.parametrize("hint,retries,waited,reason", [
    (RetryHint(31.0, False), 0, 0.0, "hint_exceeds_max_wait"),
    (RetryHint(2.0, True), 0, 0.0, "daily_quota"),
    (RetryHint(None, False), 4, 0.0, "retries_exhausted"),
    (RetryHint(None, False), 2, 55.0, "budget_exhausted"),
])
def test_waiting_is_refused_when_it_is_pointless(monkeypatch, vision_policy, hint, retries, waited, reason):
    monkeypatch.setattr(gemini_throttle, "_rng", _Rng(high=False))
    assert gemini_throttle.next_wait(POLICY, hint, retries, waited) == (None, reason)


def test_a_call_without_a_role_keeps_the_old_no_retry_contract():
    assert gemini_throttle.policy_for(None) is None
    assert gemini_throttle.next_wait(None, RetryHint(1.0, False), 0, 0.0) == (None, "no_retry")


def test_a_role_policy_comes_from_settings(vision_policy):
    assert gemini_throttle.policy_for("vision") == POLICY


# ── gemini_rest.call on a 429 ────────────────────────────────────────────────

def test_success_after_retries_returns_the_answer_and_logs_one_row(
    monkeypatch, sleeps, vision_policy, recorded_usage_rows,
):
    calls = _script(monkeypatch, [_r429("3s"), _r429(), _Resp(200, text="page")])

    assert _vision_call() == ("page", 1)
    assert len(calls) == 3
    assert sleeps == [3.0, 4.0]
    assert len(recorded_usage_rows) == 1, "a 429 carries no tokens; only the success is logged"


def test_retry_delay_is_honoured(monkeypatch, sleeps, vision_policy):
    _script(monkeypatch, [_r429("13s"), _Resp(200)])
    _vision_call()
    assert sleeps == [13.0]


def test_retry_after_header_is_honoured(monkeypatch, sleeps, vision_policy):
    _script(monkeypatch, [_r429(headers={"Retry-After": "11"}), _Resp(200)])
    _vision_call()
    assert sleeps == [11.0]


def test_exhaustion_raises_the_exception_callers_already_handle(
    monkeypatch, sleeps, vision_policy, recorded_usage_rows,
):
    calls = _script(monkeypatch, [_r429() for _ in range(5)])

    with pytest.raises(gemini_rest.GeminiQuotaExceeded) as err:
        _vision_call()

    assert err.value.reason == "retries_exhausted"
    assert len(calls) == 1 + settings.GEMINI_429_MAX_RETRIES_VISION
    assert sleeps == [2.0, 4.0, 8.0, 16.0]
    assert recorded_usage_rows == []


def test_the_budget_stops_retrying(monkeypatch, sleeps, vision_policy):
    monkeypatch.setattr(settings, "GEMINI_429_BUDGET_SECONDS_VISION", 10.0)
    calls = _script(monkeypatch, [_r429() for _ in range(5)])

    with pytest.raises(GeminiQuotaExceeded) as err:
        _vision_call()

    assert err.value.reason == "budget_exhausted"
    assert sleeps == [2.0, 4.0], "the third wait (8s) would pass the 10s budget"
    assert sum(sleeps) <= 10.0
    assert len(calls) == 3


def test_a_hint_longer_than_the_max_wait_fails_fast(monkeypatch, sleeps, vision_policy):
    calls = _script(monkeypatch, [_r429("45s")])

    with pytest.raises(GeminiQuotaExceeded) as err:
        _vision_call()

    assert err.value.reason == "hint_exceeds_max_wait"
    assert sleeps == [] and len(calls) == 1


def test_a_daily_quota_fails_fast(monkeypatch, sleeps, vision_policy):
    calls = _script(monkeypatch, [_r429("5s", quota_id=DAILY_QUOTA_ID)])

    with pytest.raises(GeminiQuotaExceeded) as err:
        _vision_call()

    assert err.value.reason == "daily_quota" and err.value.daily is True
    assert sleeps == [] and len(calls) == 1


def test_a_429_never_switches_model(monkeypatch, sleeps, vision_policy):
    monkeypatch.setattr(settings, "GEMINI_FALLBACKS_VISION", FALLBACK_MODEL)
    calls = _script(monkeypatch, [_r429(), _r429(), _Resp(200)])

    _vision_call()

    assert calls == [settings.GEMINI_MODEL] * 3


def test_the_404_fallback_still_works_after_a_429(monkeypatch, sleeps, vision_policy):
    monkeypatch.setattr(settings, "GEMINI_FALLBACKS_VISION", FALLBACK_MODEL)
    calls = _script(monkeypatch, [_r429(), _Resp(404), _Resp(200, text="fb")])

    assert _vision_call() == ("fb", 1)
    assert calls == [settings.GEMINI_MODEL, settings.GEMINI_MODEL, FALLBACK_MODEL]


def test_a_429_does_not_use_up_a_transport_attempt(monkeypatch, sleeps, vision_policy):
    monkeypatch.setattr(settings, "GEMINI_REST_MAX_RETRIES", 1)
    calls = _script(monkeypatch, [_r429(), _Resp(200)])

    _vision_call()

    assert len(calls) == 2


def test_the_call_event_reports_429_retries_and_wait(monkeypatch, sleeps, vision_policy):
    _script(monkeypatch, [_r429("3s"), _Resp(200)])
    events = []
    monkeypatch.setattr(gemini_rest.logger, "info", lambda ev, **kw: events.append((ev, kw)))

    _vision_call()

    kw = next(kw for ev, kw in events if ev == "gemini_call")
    assert kw["retries_429"] == 1
    assert kw["throttle_wait_ms"] == 3000
    assert kw["attempts"] == 2


# ── the per-role slot ────────────────────────────────────────────────────────

def test_the_slot_is_not_held_while_backing_off(monkeypatch, vision_policy):
    monkeypatch.setattr(gemini_throttle, "_rng", _Rng())
    held_during_sleep = []
    monkeypatch.setattr(gemini_throttle, "_sleep",
                        lambda s: held_during_sleep.append(gemini_throttle.in_use("vision")))
    _script(monkeypatch, [_r429(), _r429(), _Resp(200)])

    _vision_call()

    assert held_during_sleep == [0, 0]


@pytest.mark.parametrize("boom", [requests.exceptions.ConnectionError("reset"),
                                  KeyboardInterrupt()])
def test_the_slot_is_released_on_any_exception(monkeypatch, boom):
    monkeypatch.setattr(settings, "GEMINI_REST_MAX_RETRIES", 1)

    def post(*a, **kw):
        assert gemini_throttle.in_use("vision") == 1
        raise boom

    monkeypatch.setattr(requests, "post", post)

    with pytest.raises(type(boom)):
        _vision_call()
    assert gemini_throttle.in_use("vision") == 0


def test_the_slot_limit_caps_real_concurrency(monkeypatch):
    """Six threads, a limit of two: never more than two requests in flight,
    and the others are really waiting, not failing."""
    monkeypatch.setattr(settings, "GEMINI_MAX_CONCURRENCY_VISION", 2)
    release = threading.Event()
    lock = threading.Lock()
    state = {"now": 0, "peak": 0, "done": 0}

    def post(*a, **kw):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        release.wait(timeout=10)
        with lock:
            state["now"] -= 1
        return _Resp(200)

    monkeypatch.setattr(requests, "post", post)

    def worker():
        _vision_call()
        with lock:
            state["done"] += 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 10
    while gemini_throttle.in_use("vision") < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.05)  # give any unthrottled thread the chance to overshoot
    in_flight = state["now"]
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert in_flight == 2
    assert state["peak"] == 2
    assert state["done"] == 6


def test_a_saturated_slot_gives_up_after_the_call_timeout(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MAX_CONCURRENCY_TOC", 1)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Resp(200))

    with gemini_throttle.slot("toc", timeout=1):
        with pytest.raises(gemini_throttle.GeminiSlotTimeout):
            gemini_rest.call("p", api_key="K", role="toc", timeout=0.01)
    assert issubclass(gemini_throttle.GeminiSlotTimeout, gemini_rest.GeminiQuotaExceeded)


def test_the_quota_exception_is_one_class_everywhere():
    assert gemini_rest.GeminiQuotaExceeded is gemini_throttle.GeminiQuotaExceeded


def test_the_gemini_layer_never_touches_the_user_quota():
    """Retries live below the quota gates: nothing in the call layer can
    consume or check a teacher's quota, so a retry cannot charge twice."""
    import inspect

    for module in (gemini_rest, gemini_throttle, sheet_reader):
        src = inspect.getsource(module)
        for name in ("check_and_consume", "check_available", "try_consume", "app.services.quota"):
            assert name not in src, f"{module.__name__} references {name}"


# ── grading (sheet_reader) ───────────────────────────────────────────────────

FULL = '{"answers": {"1": "A", "2": "B"}}'


@pytest.fixture
def grading(monkeypatch):
    img = Image.new("RGB", (4, 4), "white")

    class _Page:
        image = img

    monkeypatch.setattr(sheet_reader, "image_to_pages", lambda b: [_Page()])
    monkeypatch.setattr(sheet_reader, "preprocess_image", lambda i: img)
    monkeypatch.setattr(settings, "GEMINI_429_BACKOFF_BASE_SECONDS", 2.0)
    monkeypatch.setattr(settings, "GEMINI_429_JITTER_FRACTION", 0.25)
    monkeypatch.setattr(settings, "GEMINI_429_MAX_RETRIES_GRADING", 2)
    monkeypatch.setattr(settings, "GEMINI_429_MAX_WAIT_SECONDS_GRADING", 8.0)
    monkeypatch.setattr(settings, "GEMINI_429_BUDGET_SECONDS_GRADING", 12.0)
    monkeypatch.setattr(gemini_throttle, "_rng", _Rng(high=False))
    slept = []

    async def asleep(seconds):
        slept.append((seconds, sheet_reader._grading_sem._value))

    monkeypatch.setattr(gemini_throttle, "_asleep", asleep)
    return slept


def _grading_script(monkeypatch, outcomes):
    """outcomes: a GeminiQuotaExceeded to raise, or a (text, finish) reply."""
    queue, calls = list(outcomes), []

    def fake(prompt, png):
        calls.append(1)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sheet_reader, "_call_sync", fake)
    return calls


def _q(seconds=None, daily=False):
    return GeminiQuotaExceeded("Gemini quota exceeded (429)", hint=RetryHint(seconds, daily))


async def test_grading_backs_off_on_a_429_instead_of_the_flat_second(monkeypatch, grading):
    """The old path: the 429 surfaced as a generic error, waited a flat 1s and
    used up one of the three READ attempts. With one read attempt allowed the
    old code gave up; now the 429 does not count against reads."""
    monkeypatch.setattr(settings, "GEMINI_GRADING_MAX_RETRIES", 1)
    calls = _grading_script(monkeypatch, [_q(3.0), (FULL, 1)])

    read = await sheet_reader.read_answer_sheet(b"x", 2)

    assert read["answers"] == {1: "A", 2: "B"}
    assert len(calls) == 2
    assert [s for s, _ in grading] == [3.0]


async def test_grading_does_not_hold_its_semaphore_while_backing_off(monkeypatch, grading):
    _grading_script(monkeypatch, [_q(), _q(), (FULL, 1)])

    await sheet_reader.read_answer_sheet(b"x", 2)

    assert [free for _, free in grading] == [settings.MAX_CONCURRENT_GRADING] * 2


async def test_grading_gives_up_on_a_daily_quota_without_waiting(monkeypatch, grading):
    calls = _grading_script(monkeypatch, [_q(2.0, daily=True), (FULL, 1)])

    read = await sheet_reader.read_answer_sheet(b"x", 2)

    assert read["answers"] == {} and read["unread"] == [1, 2], "the existing unreadable path"
    assert grading == [] and len(calls) == 1


async def test_grading_gives_up_on_a_hint_past_its_max_wait(monkeypatch, grading):
    calls = _grading_script(monkeypatch, [_q(20.0), (FULL, 1)])

    read = await sheet_reader.read_answer_sheet(b"x", 2)

    assert read["answers"] == {}
    assert grading == [] and len(calls) == 1


async def test_grading_429_retries_are_bounded(monkeypatch, grading):
    calls = _grading_script(monkeypatch, [_q() for _ in range(10)])

    read = await sheet_reader.read_answer_sheet(b"x", 2)

    assert read["answers"] == {}
    assert len(calls) == 1 + settings.GEMINI_429_MAX_RETRIES_GRADING
    assert [s for s, _ in grading] == [2.0, 4.0]


def test_the_grading_call_raises_the_quota_error_with_the_hint(monkeypatch, recorded_usage_rows):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _r429("6s"))

    with pytest.raises(GeminiQuotaExceeded) as err:
        sheet_reader._call_sync("p", b"png")

    assert err.value.hint == RetryHint(6.0, False)
    assert recorded_usage_rows == [], "a 429 carries no tokens and writes no row"


async def test_a_graded_sheet_after_429s_writes_exactly_one_usage_row(
    monkeypatch, grading, recorded_usage_rows,
):
    queue = [_r429(), _r429(), _Resp(200, text=FULL)]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: queue.pop(0))

    read = await sheet_reader.read_answer_sheet(b"x", 2)

    assert read["answers"] == {1: "A", 2: "B"}
    assert len(recorded_usage_rows) == 1


async def test_the_grading_call_event_reports_429_retries_and_wait(monkeypatch, grading):
    queue = [_r429("3s"), _Resp(200, text=FULL)]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: queue.pop(0))
    events = []
    monkeypatch.setattr(sheet_reader.logger, "info", lambda ev, **kw: events.append((ev, kw)))

    await sheet_reader.read_answer_sheet(b"x", 2)

    kw = next(kw for ev, kw in events if ev == "gemini_call")
    assert kw["retries_429"] == 1
    assert kw["throttle_wait_ms"] == 3000
