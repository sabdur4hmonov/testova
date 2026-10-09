"""Multi-user safety for PDF generation.

The bot must let 5-6+ teachers generate PDFs at the same instant without any
failing, blocking each other, or receiving another teacher's file. This proves
the builders are safe to run concurrently:

  * they hold NO per-request shared mutable state — each call uses its own
    io.BytesIO buffer, its own SimpleDocTemplate/story, and CHILD ParagraphStyles
    (never mutating the module-level STYLES, which is built once at import);
  * there are no temp files or fixed output paths to collide on (all in-memory);
  * every production call site already runs the builder via asyncio.to_thread,
    so rendering never blocks the event loop.

The test drives them exactly as production does — asyncio.to_thread under
asyncio.gather — with a unique sentinel per request, and asserts each returned
PDF contains ONLY its own sentinel (no cross-contamination) and all succeed.
"""
from __future__ import annotations

import asyncio

import fitz
import pytest

from app.services.pdf_generator import build_answer_key_pdf, build_variants_pdf


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()


def _request(i: int, q: int):
    """A uniquely-identifiable test for teacher i: a sentinel written answer in
    the key and a sentinel question in the paper, plus a mix of MCQ/open/image."""
    sentinel = f"SENTINELTOKEN{i}"
    key = {}
    questions = []
    for p in range(1, q + 1):
        if p == 1:
            key[str(p)] = [sentinel]                       # unique written answer
            questions.append({"position_in_variant": p,
                              "question_text": f"{sentinel} savol",
                              "options": {}})
        elif p % 9 == 0:
            key[str(p)] = None
            questions.append({"position_in_variant": p,
                              "question_text": f"Javobni yozing {p}", "options": {}})
        elif p % 8 == 0:
            key[str(p)] = ["A"]
            questions.append({"position_in_variant": p, "question_text": f"Rasm {p}",
                              "options": {"a": "bir", "b": "ikki", "d": "uch", "e": "trt"},
                              "correct_answer": "a", "has_image": True,
                              "image_description": "diagramma"})
        else:
            key[str(p)] = ["E"]
            questions.append({"position_in_variant": p, "question_text": f"Savol {p}",
                              "options": {"a": "bir", "b": "ikki", "d": "uch", "e": "trt"}})
    variants = [{"variant_number": 1, "answer_key": key, "questions_data": questions},
                {"variant_number": 2, "answer_key": key, "questions_data": questions}]
    return sentinel, variants, f"Exam-{sentinel}"


async def _build_both(i: int, q: int):
    sentinel, variants, title = _request(i, q)
    # exactly the production pattern: heavy ReportLab work off the event loop
    key_pdf = await asyncio.to_thread(build_answer_key_pdf, variants, title)
    var_pdf = await asyncio.to_thread(build_variants_pdf, variants, title)
    return sentinel, key_pdf, var_pdf


@pytest.mark.asyncio
async def test_six_teachers_generate_simultaneously_no_crash_or_crosstalk():
    # Mixed sizes, several at/over the ~44-question cliff that used to crash.
    sizes = [20, 33, 44, 46, 50, 60]
    results = await asyncio.gather(
        *(_build_both(i, q) for i, q in enumerate(sizes)),
        return_exceptions=True,
    )

    # 1) every request completed — no exceptions, no LayoutError.
    for r in results:
        assert not isinstance(r, BaseException), f"a concurrent build failed: {r!r}"

    sentinels = [f"SENTINELTOKEN{i}" for i in range(len(sizes))]
    for i, (sentinel, key_pdf, var_pdf) in enumerate(results):
        assert key_pdf[:4] == b"%PDF" and var_pdf[:4] == b"%PDF"
        key_txt, var_txt = _text(key_pdf), _text(var_pdf)
        # 2) each PDF carries ITS OWN sentinel...
        assert sentinel in key_txt, f"key {i} lost its own content"
        assert sentinel in var_txt, f"variants {i} lost its own content"
        # 3) ...and NONE of the other teachers' sentinels (no cross-contamination).
        for other in sentinels:
            if other != sentinel:
                assert other not in key_txt, f"key {i} leaked {other}"
                assert other not in var_txt, f"variants {i} leaked {other}"


@pytest.mark.asyncio
async def test_concurrent_builds_are_byte_identical_to_serial():
    """A concurrent build must produce the same content a serial build would —
    proving no shared state bled between the parallel renders. (Compared on
    extracted text, since embedded PDF timestamps make raw bytes differ.)"""
    _s, variants, title = _request(7, 46)
    serial = _text(build_answer_key_pdf(variants, title))
    concurrent = await asyncio.gather(
        *(asyncio.to_thread(build_answer_key_pdf, variants, title) for _ in range(6))
    )
    for pdf in concurrent:
        assert _text(pdf) == serial
