"""
Multi-Source generation — the 5 Step-5 scenarios:
(1) colliding-number two-source pool: full key per source, 3x30 round-trip=100%
(2) heavy reuse 30x30: no dup in variant, even usage (max 15, min >=14)
(3) missing image file at build: export succeeds, warning lists the question
(4) generation exception: error code + retry button, retry with same params works
(5) single-file regression is covered by tests/test_round_trip.py (kept green)
"""
import asyncio
import time

import pytest

from app.services.answer_checker import check_answers
from app.services.ai_analyzer import question_fingerprint
from app.services.variant_generator import (
    assemble_pool,
    pool_variant_builder,
    select_for_variants,
)


def _source(letter):
    """30 questions numbered 1..30 (COLLIDING across sources). The correct
    option's text is tagged so we can verify grading tracks it through shuffle."""
    qs = []
    for n in range(1, 31):
        correct = "ABCD"[n % 4]
        opts = {L: f"{letter}{n}-{L}" for L in "ABCD"}
        opts[correct] = f"{letter}{n}-CORRECT"
        qs.append({
            "question_id": f"{letter}{n}",       # globally unique
            "question_number": n,                # collides with the other source
            "question_text": f"Source {letter} question {n}",
            "options": opts,
            "correct_answer": correct,
            "has_image": False, "image_path": None,
            "group_id": None, "group_context": None,
        })
    return qs


def _colliding_pool():
    pool, collapsed, _sib = assemble_pool([_source("A"), _source("B")])
    assert len(pool) == 60 and not collapsed
    return pool


# ── (1) round-trip grading on a colliding multi-source pool ──────────────────

def test_1_colliding_pool_round_trip_100_percent():
    pool = _colliding_pool()
    t0 = time.monotonic()
    selections, _stats = select_for_variants(pool, 3, 30, seed=1)
    total, build_one = pool_variant_builder(selections, seed=1)
    variants = [build_one(i) for i in range(1, total + 1)]
    assert time.monotonic() - t0 < 5.0

    seen_ids = set()
    for v in variants:
        qd = v["questions_data"]
        assert len(qd) == 30
        # no duplicate question CONTENT within one variant
        assert len({question_fingerprint(q) for q in qd}) == 30

        for q in qd:
            pos = str(q["position_in_variant"])
            key_letter = v["answer_key"][pos]
            # the key must point at the ORIGINAL correct option's text,
            # proving grading tracks correctness through pooling + shuffle
            assert q["options"][key_letter] == f"{q['question_id']}-CORRECT"
            seen_ids.add(q["question_id"])

        # a perfect student answering per the key scores 100%
        student = dict(v["answer_key"])
        assert check_answers(student, v["answer_key"]).score_percent == 100.0

    # both sources' questions were exercised across the variants
    assert any(i.startswith("A") for i in seen_ids)
    assert any(i.startswith("B") for i in seen_ids)


# ── (2) heavy reuse: even distribution, no in-variant duplicates ─────────────

def test_2_heavy_reuse_even_and_no_dup():
    pool = _colliding_pool()
    t0 = time.monotonic()
    selections, stats = select_for_variants(pool, 30, 30, seed=2)
    assert time.monotonic() - t0 < 5.0

    assert all(len(s) == 30 for s in selections)
    usage: dict[str, int] = {}
    for s in selections:
        ids = [q["question_id"] for q in s]
        assert len(ids) == len(set(ids))  # no dup within a variant
        for qid in ids:
            usage[qid] = usage.get(qid, 0) + 1
    # 30 x 30 = 900 slots over 60 questions → exactly 15 each
    assert max(usage.values()) == 15
    assert min(usage.values()) >= 14
    assert stats["max_reuse"] == 15


# ── (3) missing image file at build → warn + render, no crash ────────────────

def test_3_missing_image_detected_and_pdf_survives(tmp_path):
    from app.services import pdf_generator as pg
    from app.bot.handlers.multi_source import _image_exists

    missing = str(tmp_path / "gone.png")
    assert _image_exists(missing) is False           # detection works
    assert _image_exists("") is False

    variant = {
        "variant_number": 1,
        "answer_key": {"1": "A"},
        "questions_data": [{
            "position_in_variant": 1,
            "question_text": "Rasmli savol",
            "options": {"A": "bir", "B": "ikki"},
            "is_open_ended": False,
            "has_image": True,
            "image_path": missing,                    # file does not exist
            "image_description": "Diagramma: CuSO4 va KOH",
        }],
    }
    # build must NOT raise on a missing image file
    pdf = pg.build_variants_pdf([variant], "Test")
    assert pdf[:4] == b"%PDF"


# ── (4) generation exception → error code + retry with same params ───────────

class _FakeMsg:
    def __init__(self):
        self.texts = []
        self.markups = []
    async def answer(self, text, **kw):
        self.texts.append(text)
        self.markups.append(kw.get("reply_markup"))
        return self
    async def edit_text(self, text, **kw):
        self.texts.append(text)
        self.markups.append(kw.get("reply_markup"))
    async def answer_document(self, *a, **k):
        pass


class _FakeState:
    def __init__(self, data):
        self._d = dict(data)
    async def get_data(self):
        return dict(self._d)
    async def update_data(self, **kw):
        self._d.update(kw)
    async def set_state(self, s):
        self._d["__state__"] = s


class _User:
    id = "u1"
    language = type("L", (), {"value": "uz"})()


def test_4_generation_error_code_and_retry(monkeypatch):
    from app.bot.handlers import multi_source as ms

    calls = {"n": 0}

    async def flaky_do_generate(message, state, db_user, session_id, n, m, status, lang):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")   # first attempt fails
        await message.answer("OK generated")   # second attempt succeeds

    monkeypatch.setattr(ms, "_do_generate", flaky_do_generate)

    msg = _FakeMsg()
    state = _FakeState({"builder_session_id": "s1", "n_variants": 3, "m_per_variant": 30})

    # First attempt → error path
    asyncio.run(ms._generate_from_pool(msg, state, _User()))
    err_text = next(t for t in msg.texts if "GEN-" in t)
    assert "#GEN-" in err_text
    # params preserved for retry
    d = asyncio.run(state.get_data())
    assert d["n_variants"] == 3 and d["m_per_variant"] == 30
    # a retry keyboard was offered
    assert any(mk is not None for mk in msg.markups)

    # Pressing "Qayta urinish" → retry with the SAME params succeeds
    asyncio.run(ms._generate_from_pool(msg, state, _User()))
    assert calls["n"] == 2
    assert "OK generated" in msg.texts
