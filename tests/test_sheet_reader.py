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

    def set_response(text: str, finish_reason: int = 1):
        def fake(prompt, png):
            calls.append(prompt)
            return text, finish_reason   # _call_sync now returns (text, finish_reason)
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
        "answers": {}, "texts": {}, "unclear": [],
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


async def test_cyrillic_answer_kept_in_real_script(patched):
    # Gemini echoes Cyrillic А/В — the REAL character is stored (so the report
    # shows "В", not "B"); equality with the Latin look-alike is applied at
    # comparison time by checker.is_correct.
    patched('{"answers": {"1":"А","2":"В"}}')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["answers"] == {1: "А", 2: "В"}
    from app.services.checker import is_correct
    assert is_correct(res["answers"][1], ["A"])   # Cyrillic А == Latin A
    assert is_correct(res["answers"][2], ["B"])   # Cyrillic В == Latin B


async def test_cyrillic_be_never_collides_with_ve(patched):
    # THE OFF-BY-ONE GUARD: Cyrillic Б (option 2) must NEVER grade equal to
    # Cyrillic В (option 3) or to Latin B — that collision silently credited a
    # wrong answer on Cyrillic tests.
    patched('{"answers": {"1":"Б","2":"В"}}')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["answers"] == {1: "Б", 2: "В"}
    from app.services.checker import is_correct
    assert not is_correct(res["answers"][1], ["В"])   # Б != В
    assert not is_correct(res["answers"][1], ["B"])   # Б != Latin B
    assert is_correct(res["answers"][1], ["Б"])       # Б == Б


async def test_invalid_letter_dropped(patched):
    patched('{"answers": {"1":"A","2":"Z"}}')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["answers"] == {1: "A"}   # Z is neither valid nor "?"
    assert res["unclear"] == []


async def test_ef_read_as_marked_options(patched):
    # E and F are real option letters (tests use up to F, often gapped a,b,d,e).
    # Both must land in answers as MARKED OPTIONS — not dropped, not routed to
    # written text. Pins the photo-path fix for the E/F "Aniqlanmadi" bug.
    patched('{"answers": {"1":"E","12":"F","3":"D"}}')
    res = await SR.read_answer_sheet(b"x", 12)
    assert res["answers"] == {1: "E", 12: "F", 3: "D"}
    assert res["texts"] == {}


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


async def test_unclear_not_regressed_by_name_flag(patched):
    # The "?" marked-letter path is untouched by the name confidence field.
    patched('{"answers": {"1":"?","2":"B"}, "name_unsure": false}')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["unclear"] == [1]
    assert res["answers"] == {2: "B"}


async def test_name_flag_rides_one_gemini_call(patched):
    patched('{"student_name": "X", "name_unsure": true, "answers": {"22": "PHONE"}}')
    await SR.read_answer_sheet(b"x", 22)
    # ONE read per sheet: the name flag and the answers ride the SAME prompt.
    assert len(patched.calls) == 1


# ── Empty/truncation retry (the false "unclear photo" fix) ───────────────────

def _stub_image(monkeypatch):
    img = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(SR, "image_to_pages", lambda b: [_Page(img)])
    monkeypatch.setattr(SR, "preprocess_image", lambda i: img)

    async def _no_sleep(_):
        return None
    monkeypatch.setattr(SR.asyncio, "sleep", _no_sleep)  # keep the test instant


async def test_empty_read_retries_then_succeeds(monkeypatch):
    # A truncated/empty first draw (finish_reason=2) is RETRIED; a later full read
    # is used — a good photo is never falsely rejected on one unlucky draw.
    _stub_image(monkeypatch)
    seq = [("", 2), ("", 2), ('{"answers": {"1":"A","2":"B"}}', 1)]
    calls = {"n": 0}

    def fake(prompt, png):
        i = calls["n"]; calls["n"] += 1
        return seq[i]
    monkeypatch.setattr(SR, "_call_sync", fake)

    res = await SR.read_answer_sheet(b"x", 2)
    assert res["answers"] == {1: "A", 2: "B"}
    assert calls["n"] > 2   # empties were retried until content came back


async def test_all_empty_reads_return_empty(monkeypatch):
    # If EVERY attempt is empty, the read is empty and the caller shows the retake
    # message — the genuinely-unreadable case still degrades gracefully.
    _stub_image(monkeypatch)
    calls = {"n": 0}

    def fake(prompt, png):
        calls["n"] += 1
        return "", 2   # always truncated/empty
    monkeypatch.setattr(SR, "_call_sync", fake)

    res = await SR.read_answer_sheet(b"x", 25)
    assert res["answers"] == {} and res["texts"] == {} and res["unclear"] == []
    # 2 parallel reads x (1 initial + 2 retries) = 6 attempts, then unreadable.
    assert calls["n"] == 3   # 1 initial + 2 retries (GEMINI_GRADING_MAX_RETRIES)


async def test_full_25_answer_read_not_retried(monkeypatch):
    # A clean full read on the first attempt is used immediately — a 25-answer
    # sheet is not truncated (8192 cap) and costs exactly one Gemini call.
    _stub_image(monkeypatch)
    full = '{"answers": {' + ",".join(f'"{i}":"A"' for i in range(1, 26)) + "}}"
    calls = {"n": 0}

    def fake(prompt, png):
        calls["n"] += 1
        return full, 1
    monkeypatch.setattr(SR, "_call_sync", fake)

    res = await SR.read_answer_sheet(b"x", 25)
    assert len(res["answers"]) == 25
    assert calls["n"] == 1   # one call, no retry needed
