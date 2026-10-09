"""AUDIT C-4 — batch scheme transcription by PAGE, not one call per question.

`ensure_scheme_content`'s Gemini rung sent the whole page image once per
question needing scheme content: two questions sharing a page cost two
full-page-image calls, parsed twice, serially. `_recover_missing_options` in
the same module already solves the identical problem correctly — group by page
first, list every affected question number in one prompt, get a JSON array back
keyed by question number — so this is that proven shape applied one rung over.

The cost is worst exactly where the fallback matters most: on DOCX/image
uploads `pdf_bytes` is None, so the free geometric re-crop rung is skipped and
EVERY figure question falls through to Gemini.
"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from app.services.ai_analyzer import AIAnalyzer


def _analyzer():
    return AIAnalyzer.__new__(AIAnalyzer)   # skip Gemini client init


def _images(n: int) -> list[Image.Image]:
    return [Image.new("RGB", (64, 64), "white") for _ in range(n)]


def _scheme_q(n: int, page: int) -> dict:
    """A question that NEEDS scheme content and does not yet HAVE it, so it
    reaches the Gemini rung. has_image with no image_path is the real shape a
    figure question arrives in."""
    return {
        "question_number": n, "section": 1, "page_number": page,
        "question_text": f"{n}-masala: sxemani to'ldiring.",
        "options": {"A": "1", "B": "2"},
        "has_image": True,
        "image_description": None,
    }


def _chain_for(n: int) -> str:
    """A reply the acceptance gate takes: an arrow chain carrying a formula."""
    return f"CaO{n} →(H2O) Ca(OH)2 →(CO2) CaCO3"


def _spy(monkeypatch, analyzer, reply_for):
    """Record every _call_multi and answer with `reply_for(prompt)`."""
    calls = []

    async def fake_call_multi(prompt, imgs, call_site=None):
        calls.append({"prompt": prompt, "n_images": len(imgs),
                      "call_site": call_site})
        return reply_for(prompt), 1

    monkeypatch.setattr(analyzer, "_call_multi", fake_call_multi)
    return calls


def _array_reply(nums):
    return lambda prompt: json.dumps(
        [{"n": n, "chain": _chain_for(n), "desc": ""} for n in nums],
        ensure_ascii=False,
    )


async def test_two_questions_on_one_page_cost_exactly_one_call(monkeypatch):
    an = _analyzer()
    qs = [_scheme_q(11, page=1), _scheme_q(12, page=1)]
    calls = _spy(monkeypatch, an, _array_reply([11, 12]))

    await an.ensure_scheme_content(qs, images=_images(1))

    assert len(calls) == 1, (
        f"one page must cost one call, not one per question (saw {len(calls)})")
    assert calls[0]["n_images"] == 1
    for q in qs:
        assert "→" in q["question_text"], (
            f"question {q['question_number']} did not get its chain applied")


async def test_both_question_numbers_are_named_in_the_single_prompt(monkeypatch):
    an = _analyzer()
    qs = [_scheme_q(11, page=1), _scheme_q(12, page=1)]
    calls = _spy(monkeypatch, an, _array_reply([11, 12]))

    await an.ensure_scheme_content(qs, images=_images(1))

    prompt = calls[0]["prompt"]
    assert "11" in prompt and "12" in prompt, (
        "the batched prompt must list every question number on that page")


async def test_questions_on_different_pages_cost_one_call_per_page(monkeypatch):
    an = _analyzer()
    qs = [_scheme_q(11, page=1), _scheme_q(12, page=1), _scheme_q(20, page=2)]
    calls = _spy(monkeypatch, an, _array_reply([11, 12, 20]))

    await an.ensure_scheme_content(qs, images=_images(2))

    assert len(calls) == 2, (
        f"3 questions over 2 pages must cost 2 calls, saw {len(calls)}")


async def test_a_single_question_alone_on_its_page_still_works(monkeypatch):
    """The array-of-one case: the old prompt returned a bare object, so this is
    the shape most at risk from the rewrite."""
    an = _analyzer()
    q = _scheme_q(7, page=1)
    _spy(monkeypatch, an, _array_reply([7]))

    failed = await an.ensure_scheme_content([q], images=_images(1))

    assert "→" in q["question_text"]
    assert q["has_image"] is False
    assert failed == []


async def test_a_question_the_reply_omits_is_reported_not_silently_dropped(
    monkeypatch,
):
    """One page, two questions, the model answers for only one. The other must
    end up in the returned teacher warning, not vanish."""
    an = _analyzer()
    qs = [_scheme_q(11, page=1), _scheme_q(12, page=1)]
    _spy(monkeypatch, an, _array_reply([11]))          # 12 omitted

    failed = await an.ensure_scheme_content(qs, images=_images(1))

    assert "→" in qs[0]["question_text"]
    assert (1, 12) in failed
    assert (1, 11) not in failed


async def test_one_page_failing_does_not_abort_the_other_pages(monkeypatch):
    """Best-effort per page, exactly like _recover_missing_options: a raised
    call on page 1 must not cost page 2 its recovery."""
    an = _analyzer()
    qs = [_scheme_q(11, page=1), _scheme_q(20, page=2)]
    calls = []

    async def flaky(prompt, imgs, call_site=None):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("page 1 call blew up")
        return json.dumps([{"n": 20, "chain": _chain_for(20), "desc": ""}]), 1

    monkeypatch.setattr(an, "_call_multi", flaky)

    failed = await an.ensure_scheme_content(qs, images=_images(2))

    assert len(calls) == 2, "page 2 must still be attempted after page 1 raised"
    assert "→" in qs[1]["question_text"]
    assert (1, 11) in failed


async def test_a_question_that_already_has_content_makes_no_call(monkeypatch):
    """Cost guard: the gate that skips satisfied questions must run BEFORE the
    batching, not after it."""
    an = _analyzer()
    done = _scheme_q(11, page=1)
    done["question_text"] = "11-masala: CaO →(H2O) Ca(OH)2"
    calls = _spy(monkeypatch, an, _array_reply([11]))

    await an.ensure_scheme_content([done], images=_images(1))

    assert calls == []
