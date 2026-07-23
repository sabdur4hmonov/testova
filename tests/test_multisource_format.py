"""
Multi-source builder: Oddiy/Ixcham format choice reuses the single-upload
mechanism (pdf_format FSM key), and build_variants_pdf_compact works on
MULTI-SOURCE pool variants (the coverage gap the handoff flagged).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

import app.bot.handlers.multi_source as MS
from app.services.pdf_generator import (
    MARGIN, build_variants_pdf, build_variants_pdf_compact,
)


def test_builder_format_state_exists():
    from app.bot.states.forms import BuilderStates
    assert hasattr(BuilderStates, "waiting_for_builder_format")


# ── compact renders MULTI-SOURCE pool variants (the coverage gap) ────────────

def _multisource_variants():
    # merged pool: math from one "file", a figure question from another
    qd = [
        {"position_in_variant": 1, "question_number": 5,
         "question_text": "Hisoblang: (1)/(2) + sqrt(3) ni toping.",
         "options": {"A": "1", "B": "(8)/(5)", "C": "2", "D": "3"}},
        {"position_in_variant": 2, "question_number": 8,
         "question_text": "Rasmda A va B nuqtalar tasvirlangan.",
         "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
         "has_image": True, "image_path": None,
         "image_description": "A number line with points A and B."},
    ]
    return [{"variant_number": 1, "questions_data": qd},
            {"variant_number": 2, "questions_data": qd}]


def test_compact_renders_multisource_pool_variants():
    pytest.importorskip("matplotlib")
    fitz = pytest.importorskip("fitz")
    pdf = build_variants_pdf_compact(_multisource_variants(), "Ko'p manbadan")
    assert pdf[:4] == b"%PDF"
    d = fitz.open(stream=pdf, filetype="pdf")
    assert d.page_count >= 2
    imgs = sum(1 for p in d for b in p.get_text("rawdict")["blocks"] if b.get("type") == 1)
    pg = next(p for p in d if "Variant 2" in p.get_text())
    # Topmost block, not the "Variant 2" text: the ported compact header prints
    # the fill-in fields first and "Variant N" below them, so the label is no
    # longer topmost. The property under test is the fresh page.
    blk = min((b for b in pg.get_text("blocks") if b[6] == 0), key=lambda b: b[1])
    d.close()
    assert imgs >= 1, "math should route through math_render (image markup)"
    assert blk[1] < MARGIN + 20, "each variant must start on a fresh page"


def test_standard_renders_same_multisource_fixture():
    pdf = build_variants_pdf(_multisource_variants(), "Ko'p manbadan")
    assert pdf[:4] == b"%PDF"


# ── _do_generate routes to the builder chosen via pdf_format ─────────────────

class _Stop(Exception):
    """Sentinel raised by the fake builder to halt _do_generate before DB writes."""


def _run_route(monkeypatch, data: dict) -> list[str]:
    rec: list[str] = []

    def fake_std(variants, title):
        rec.append("standard")
        raise _Stop()

    def fake_compact(variants, title):
        rec.append("compact")
        raise _Stop()

    monkeypatch.setattr(MS, "build_variants_pdf", fake_std)
    monkeypatch.setattr(MS, "build_variants_pdf_compact", fake_compact)

    async def fake_load(session_id):
        return ([{"question_number": 1}], [], [], [])
    monkeypatch.setattr(MS, "_load_pool", fake_load)
    monkeypatch.setattr(
        MS, "select_for_variants",
        lambda pool, n, m: ([[{"question_number": 1}]],
                            {"max_reuse": 1, "reused_count": 0, "reused_numbers": []}),
    )

    def fake_builder(selections):
        def build_one(i):
            return {"variant_number": i,
                    "questions_data": [{"question_number": 1, "question_text": "x",
                                        "options": {"A": "1"}}],
                    "question_order": [1], "option_mapping": {}, "answer_key": {"1": "A"}}
        return len(selections), build_one
    monkeypatch.setattr(MS, "pool_variant_builder", fake_builder)

    state = AsyncMock()
    state.get_data = AsyncMock(return_value=data)
    try:
        asyncio.run(MS._do_generate(
            AsyncMock(), state, Mock(), "sess", 1, 1, AsyncMock(), "uz",
        ))
    except _Stop:
        pass
    return rec


def test_routes_to_compact_when_chosen(monkeypatch):
    assert _run_route(monkeypatch, {"pdf_format": "compact"}) == ["compact"]


def test_routes_to_standard_when_chosen(monkeypatch):
    assert _run_route(monkeypatch, {"pdf_format": "standard"}) == ["standard"]


def test_absent_pdf_format_falls_back_to_standard(monkeypatch):
    assert _run_route(monkeypatch, {}) == ["standard"]
