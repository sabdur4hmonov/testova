"""Telegram handwriting -> text (training/transcribe_telegram.py), no network."""
from __future__ import annotations

import io
import json
import random
from pathlib import Path

import pytest

from detector.training import generate_ai, prepare_human, split, transcribe_telegram as tt
from detector.training import common
from detector.training.common import read_jsonl

WORDS = ("maktab kitob bahor daraxt shahar daryo ota ona do'st o'qituvchi bog' qishloq "
         "tog' bayram dars yo'l non suv osmon quyosh texnologiya pul bank karta").split()


def essay(seed: int, n: int = 200) -> str:
    rng = random.Random(seed)
    out, sent = [], []
    for i in range(n):
        sent.append(rng.choice(WORDS))
        if len(sent) == 9:
            s = " ".join(sent)
            out.append(s[0].upper() + s[1:] + ".")
            sent = []
    return " ".join(out)


def jpg(color: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), (color, color, color)).save(buf, "JPEG")
    return buf.getvalue()


def album(aid, pages, posted, canonical=None, complete=True, topic="Elektron to'lovlar"):
    return {"id": aid, "canonical_album": canonical or aid, "posted_at": posted, "topic": topic,
            "script": "lotin", "complete_download": complete, "source": f"https://t.me/x/{aid}",
            "pages": [{"photo_id": p, "page_index": i + 1, "file": f"telegram_images/tg_{p:05d}.jpg"}
                      for i, p in enumerate(pages)]}


@pytest.fixture
def data(tmp_path: Path) -> Path:
    human = tmp_path / "human"
    (human / "telegram_images").mkdir(parents=True)
    albums = [
        album(1, [1, 2], "2021-05-01T10:00:00+00:00"),            # toza
        album(3, [3], "2026-04-01T10:00:00+00:00"),               # tasdiqlanmagan
        album(4, [4], "2026-04-02T10:00:00+00:00"),               # too short
        album(5, [5], "2026-04-03T10:00:00+00:00"),               # too many [?]
        album(6, [6], "2026-04-04T10:00:00+00:00", complete=False),
        album(7, [7], "2026-04-05T10:00:00+00:00"),               # near-duplicate of 3
        album(8, [1, 2], "2026-04-06T10:00:00+00:00", canonical=1),  # re-post: skipped
        album(9, [9], "2020-01-01T10:00:00+00:00"),               # toza
        album(10, [10], "2026-05-01T10:00:00+00:00"),
        album(11, [11], "2021-01-01T10:00:00+00:00"),             # toza
    ]
    # pages listed out of order on purpose
    albums[0]["pages"].reverse()
    for a in albums:
        for p in a["pages"]:
            (human / p["file"]).write_bytes(jpg(p["photo_id"] * 10 % 255))
    (human / "telegram_albums.json").write_text(json.dumps(albums), encoding="utf-8")
    return tmp_path


def fake_answers():
    return {
        1: {"task": "Elektron to'lovlar haqida fikringizni yozing.",
            "names": ["Karimova Dilnoza"],
            "essay": "Karimova Dilnoza\n\nElektron to'lovlar haqida fikringizni yozing.\n\n" + essay(1)},
        3: {"task": "", "names": [], "essay": essay(3)},
        4: {"task": "", "names": [], "essay": essay(4, 60)},
        5: {"task": "", "names": [], "essay": " ".join(
            w if i % 10 else "[?]" for i, w in enumerate(essay(5).split()))},
        6: {"task": "", "names": [], "essay": essay(6)},
        7: {"task": "", "names": [], "essay": essay(3) + " Yana bir gap."},
        9: {"task": "", "names": [], "essay": essay(9)},
        10: {"task": "", "names": [], "essay": essay(10)},
        11: {"task": "", "names": [], "essay": essay(11)},
    }


class FakeVision:
    def __init__(self, data_dir):
        self.calls: list[list[bytes]] = []
        self.answers = fake_answers()
        self.data = data_dir
        # map first page bytes -> album id
        albums = json.loads((data_dir / "human" / "telegram_albums.json").read_text())
        self.by_first = {}
        for a in albums:
            if a["canonical_album"] != a["id"]:
                continue
            first = sorted(a["pages"], key=lambda p: p["page_index"])[0]
            self.by_first[(data_dir / "human" / first["file"]).read_bytes()] = a["id"]

    def __call__(self, images):
        self.calls.append(images)
        return dict(self.answers[self.by_first[images[0]]])


class P(generate_ai.Pacer):
    def __init__(self):
        self.slept = []
        super().__init__(1000, clock=lambda: 0.0, sleep=self.slept.append)


def run_all(data):
    fake = FakeVision(data)
    tt.transcribe(data, key="k", model="m", rpm=1000, max_calls=100, call=fake, pacer=P(),
                  log=lambda _m: None)
    return fake


def test_prompt_orders_verbatim_copy():
    p = tt.PROMPT
    for must in ("EXACTLY", "do NOT correct", "[?]", '"task"', '"names"', '"essay"', "do NOT add"):
        assert must in p


def test_albums_canonical_and_page_order(data):
    albums = tt.load_albums(data)
    assert [a["id"] for a in albums] == [1, 3, 4, 5, 6, 7, 9, 10, 11]   # re-post 8 skipped
    a1 = albums[0]
    assert [p["page_index"] for p in a1["pages"]] == [1, 2]


def test_transcribe_sends_pages_in_order_and_caches(data):
    fake = run_all(data)
    assert len(fake.calls) == 9
    first = [c for c in fake.calls if len(c) == 2][0]
    human = data / "human"
    assert first == [(human / "telegram_images/tg_00001.jpg").read_bytes(),
                     (human / "telegram_images/tg_00002.jpg").read_bytes()]
    assert len(list((data / "work" / "transcripts").glob("*.json"))) == 9


def test_transcribe_resumes_without_repeating(data):
    fake1 = FakeVision(data)
    s1 = tt.transcribe(data, key="k", model="m", rpm=1000, max_calls=3, call=fake1, pacer=P(),
                       log=lambda _m: None)
    assert s1["written"] == 3
    fake2 = FakeVision(data)
    s2 = tt.transcribe(data, key="k", model="m", rpm=1000, max_calls=100, call=fake2, pacer=P(),
                       log=lambda _m: None)
    assert s2["cached_before"] == 3 and len(fake2.calls) == 6


def test_transcribe_waits_on_429(data):
    fake = FakeVision(data)
    calls = {"n": 0}

    def flaky(images):
        calls["n"] += 1
        if calls["n"] == 1:
            raise generate_ai.RateLimited(42)
        return fake(images)
    pacer = P()
    tt.transcribe(data, key="k", model="m", rpm=1000, max_calls=100, call=flaky, pacer=pacer,
                  log=lambda _m: None)
    assert max(pacer.slept) >= 42


def test_build_filters_masks_dedupes_and_labels(data):
    run_all(data)
    rep = tt.build(data)
    reasons = {r["album"]: r["reason"] for r in rep["rejected"]}
    assert reasons[4].startswith("too_short")
    assert reasons[5].startswith("unreadable")
    assert reasons[6] == "incomplete_album"
    assert any(r["reason"] == "near_duplicate" for r in rep["rejected"])
    assert rep["kept"] == 5 and rep["toza"] == 3 and rep["tasdiqlanmagan"] == 2
    t1 = (data / "human" / "tg_essay_00001.txt").read_text(encoding="utf-8")
    assert "Karimova" not in t1 and "Dilnoza" not in t1             # names out
    assert "fikringizni yozing" not in t1                            # task out
    meta = (data / "human" / "meta_telegram.csv").read_text(encoding="utf-8")
    assert "tg_essay_00001.txt;;insho;lotin;Elektron to'lovlar;;toza;2021-05-01" in meta


def test_status_rule():
    assert tt.status_for("2022-10-31T23:00:00+00:00") == "toza"
    assert tt.status_for("2022-11-01T00:00:00+00:00") == "tasdiqlanmagan"
    assert tt.status_for(None) == "tasdiqlanmagan"


def test_only_clean_essays_reach_the_test_split(data):
    run_all(data)
    tt.build(data)
    prepare_human.process(data, 120, 350)
    chunks = list(read_jsonl(data / "work" / "human_chunks.jsonl"))
    assert {c["status"] for c in chunks} == {"toza", "tasdiqlanmagan"}
    # a large test share makes the rule visible on tiny data
    split.run(data, 0.5, 0.1, 13)
    test = list(read_jsonl(data / "work" / "split" / "test.jsonl"))
    status = {c["doc_id"]: c["status"] for c in chunks}
    assert test and all(status[r["group"]] == "toza" for r in test)


def test_review_page(data):
    run_all(data)
    tt.build(data)
    out = tt.review(data, n=20)
    page = out.read_text(encoding="utf-8")
    assert page.count('class="card"') == 5                     # all kept (< 20)
    assert '<img src="../human/telegram_images/' in page
    assert "<script" not in page


def test_key_resolution(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=bot-key\nDETECTOR_GEN_GEMINI_KEY=free-key\n", encoding="utf-8")
    monkeypatch.delenv("DETECTOR_GEN_GEMINI_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert common.resolve_gen_key(env) == "free-key"
    env.write_text("GEMINI_API_KEY=same\nDETECTOR_GEN_GEMINI_KEY=same\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        common.resolve_gen_key(env)
    env.write_text("GEMINI_API_KEY=bot\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        common.resolve_gen_key(env)
