"""sheet_reader: defensive parse of Gemini output (good / fenced / malformed)."""
from __future__ import annotations

import pytest
from PIL import Image

from app.services import sheet_reader as SR


class _Page:
    def __init__(self, img):
        self.image = img


@pytest.fixture
def patched(monkeypatch):
    """Skip real decode/deskew and the real Gemini call — the unit under test
    is the parse + normalization, not image I/O."""
    img = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(SR, "image_to_pages", lambda b: [_Page(img)])
    monkeypatch.setattr(SR, "preprocess_image", lambda i: img)

    calls = []

    def set_response(text: str):
        def fake(prompt, png):
            calls.append(prompt)
            return text
        monkeypatch.setattr(SR, "_call_sync", fake)

    set_response.calls = calls
    return set_response


async def test_good_json(patched):
    patched('{"variant": 3, "answers": {"1":"A","2":"?","3":null,"4":"C"}}')
    res = await SR.read_answer_sheet(b"x", 4)
    assert res["variant"] == 3
    assert res["answers"] == {1: "A", 4: "C"}
    assert res["unclear"] == [2]


async def test_fenced_json(patched):
    patched('```json\n{"variant": null, "answers": {"1":"B","2":"D"}}\n```')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["variant"] is None
    assert res["answers"] == {1: "B", 2: "D"}
    assert res["unclear"] == []


async def test_malformed_safe_failure(patched):
    patched("this is not json at all")
    res = await SR.read_answer_sheet(b"x", 10)
    assert res == {
        "variant": None, "student_name": None, "name_unclear": False,
        "answers": {}, "texts": {}, "low_confidence": [], "unclear": [],
    }


async def test_reads_student_name_and_variant(patched):
    patched('{"variant": 3, "student_name": "Ali Valiyev", "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["student_name"] == "Ali Valiyev"
    assert res["variant"] == 3


async def test_name_null_is_none(patched):
    patched('{"variant": null, "student_name": null, "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["student_name"] is None


async def test_name_missing_key_is_none(patched):
    patched('{"variant": 1, "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["student_name"] is None


async def test_name_returned_raw_not_normalized(patched):
    # Odd casing/spacing and Cyrillic script must be preserved EXACTLY — the
    # name is never spell-corrected, case-folded, or transliterated.
    patched('{"student_name": "aliycha  QODIROVA", "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["student_name"] == "aliycha  QODIROVA"


async def test_salvage_trailing_prose(patched):
    patched('Here you go: {"answers": {"1":"A"}} hope it helps')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["answers"] == {1: "A"}


async def test_cyrillic_answer_folded(patched):
    # Gemini echoes a Cyrillic А — must fold to Latin A.
    patched('{"answers": {"1":"А","2":"В"}}')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["answers"] == {1: "A", 2: "B"}


async def test_invalid_letter_dropped(patched):
    patched('{"answers": {"1":"A","2":"Z"}}')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["answers"] == {1: "A"}   # Z is neither valid nor "?"
    assert res["unclear"] == []


async def test_variant_from_text(patched):
    patched('{"variant": "Variant 5", "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["variant"] == 5


# ── Confidence flags (Part 1: uncertainty flagging) ──────────────────────────

async def test_name_flagged_unsure(patched):
    patched('{"student_name": "SANJARBEK", "name_unsure": true, "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["name_unclear"] is True
    assert res["student_name"] == "SANJARBEK"   # best guess still returned


async def test_name_not_flagged_when_confident(patched):
    patched('{"student_name": "ALI", "name_unsure": false, "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["name_unclear"] is False


async def test_name_unsure_absent_defaults_false(patched):
    patched('{"student_name": "ALI", "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["name_unclear"] is False


async def test_name_unsure_but_no_name_is_not_unclear(patched):
    # A missing name is "ask for it" (existing prompt), not "confirm a doubtful read".
    patched('{"student_name": null, "name_unsure": true, "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["name_unclear"] is False
    assert res["student_name"] is None


async def test_name_unsure_string_false_is_false(patched):
    # bool("false") is True — the reader must not flag every name. Guarded.
    patched('{"student_name": "ALI", "name_unsure": "false", "answers": {"1":"A"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["name_unclear"] is False


async def test_written_answer_flagged_low_confidence(patched):
    patched('{"answers": {"22": "SMARTPHONE"}, "unsure": [22]}')
    res = await SR.read_answer_sheet(b"x", 22)
    assert res["low_confidence"] == [22]
    assert res["texts"] == {22: "SMARTPHONE"}   # best guess still returned


async def test_written_answer_not_flagged(patched):
    patched('{"answers": {"22": "SMARTPHONE"}}')
    res = await SR.read_answer_sheet(b"x", 22)
    assert res["low_confidence"] == []
    assert res["texts"] == {22: "SMARTPHONE"}


async def test_unsure_string_numbers_coerced(patched):
    patched('{"answers": {"22": "PHONE"}, "unsure": ["22"]}')
    res = await SR.read_answer_sheet(b"x", 22)
    assert res["low_confidence"] == [22]


async def test_unsure_marked_letter_is_ignored(patched):
    # A flagged MARKED-letter question is NOT low_confidence (letters use "?").
    patched('{"answers": {"1": "A"}, "unsure": [1]}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["low_confidence"] == []
    assert res["answers"] == {1: "A"}


async def test_unclear_not_regressed_by_flags(patched):
    # The "?" marked-letter path is untouched by the new confidence fields.
    patched('{"answers": {"1":"?","2":"B"}, "unsure": [], "name_unsure": false}')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["unclear"] == [1]
    assert res["answers"] == {2: "B"}
    assert res["low_confidence"] == []


async def test_flags_ride_one_gemini_call(patched):
    patched('{"student_name": "X", "name_unsure": true, '
            '"answers": {"22": "PHONE"}, "unsure": [22]}')
    await SR.read_answer_sheet(b"x", 22)
    assert len(patched.calls) == 1   # name + answer flags from the SAME call
