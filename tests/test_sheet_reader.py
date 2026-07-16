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

    def set_response(text: str):
        monkeypatch.setattr(SR, "_call_sync", lambda prompt, png: text)

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
    assert res == {"variant": None, "student_name": None, "answers": {}, "unclear": []}


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
