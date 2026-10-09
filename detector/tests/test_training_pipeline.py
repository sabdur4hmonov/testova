"""A2 pipeline end to end on tiny synthetic data — no network, no model."""
from __future__ import annotations

import csv
import json
import random
import re
from pathlib import Path

import pytest

from detector.training import augment_ocr, evaluate, generate_ai, prepare_human, split
from detector.training.common import read_jsonl, write_jsonl

VOCAB = {
    "uz": ("va bu bilan uchun ham bir lekin juda keyin shu".split(),
           "maktab kitob bahor daraxt shahar daryo ota ona do'st o'qituvchi bog' qishloq "
           "tog' bayram dars yo'l non suv osmon quyosh".split(),
           "keldi ketdi ko'rdi o'qidi yozdi aytdi bordi o'ynadi yashadi ishladi".split()),
    "ru": ("и в не на что с как но это для".split(),
           "школа книга весна дерево город река отец мать друг учитель сад деревня гора "
           "праздник урок дорога хлеб вода небо солнце".split(),
           "пришёл ушёл увидел читал писал сказал пошёл играл жил работал".split()),
    "en": ("the and of to in that with for this was".split(),
           "school book spring tree city river father mother friend teacher garden village "
           "mountain holiday lesson road bread water sky sun".split(),
           "came left saw read wrote said went played lived worked".split()),
}


def make_text(lang: str, n_words: int, seed: int) -> str:
    stop, nouns, verbs = VOCAB[lang]
    rng = random.Random(seed)
    sents, count = [], 0
    while count < n_words:
        words = [rng.choice(nouns), rng.choice(stop), rng.choice(nouns), rng.choice(verbs),
                 rng.choice(stop), rng.choice(nouns), rng.choice(stop), rng.choice(nouns)]
        s = " ".join(words)
        sents.append(s[0].upper() + s[1:] + ".")
        count += len(words)
    paras = [" ".join(sents[i:i + 6]) for i in range(0, len(sents), 6)]
    return "\n\n".join(paras)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    human = tmp_path / "human"
    human.mkdir()
    uz = ("O'ZBEKISTON RESPUBLIKASI XALQ TA'LIMI VAZIRLIGI\n\nMavzu: Bahor fasli\n\n"
          "Bajardi: Karimov Aziz\n\n" + make_text("uz", 700, 1)
          + "\n\nMuallif bilan bog'lanish: aziz@example.com, +998 90 123 45 67.")
    (human / "uz1.txt").write_text(uz, encoding="utf-8")
    (human / "uz1_copy.txt").write_text(uz, encoding="utf-8")         # exact duplicate
    (human / "uz2.txt").write_text(make_text("uz", 500, 2), encoding="utf-8")
    (human / "uz3.txt").write_text(make_text("uz", 500, 3), encoding="utf-8")

    from docx import Document
    doc = Document()
    for p in make_text("ru", 600, 4).split("\n\n"):
        doc.add_paragraph(p)
    doc.save(str(human / "ru1.docx"))
    (human / "ru2.txt").write_text(make_text("ru", 500, 5), encoding="utf-8")
    (human / "ru3.txt").write_text(make_text("ru", 500, 6), encoding="utf-8")

    import fitz
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 560, 800), make_text("en", 400, 7), fontsize=8)
    pdf.save(str(human / "en1.pdf"))

    (human / "meta.csv").write_text(
        "fayl;yil;tur;yozuv;mavzu;ismlar\n"
        "uz1.txt;2019;insho;lotin;;Aziz\n"
        "uz1_copy.txt;2019;insho;lotin;;\n"
        "uz2.txt;2018;insho;lotin;Qishloq hayoti;\n"
        "uz3.txt;2018;referat;lotin;Daryolar;\n"
        "ru1.docx;2017;insho;kirill;Весна;\n"
        "ru2.txt;2016;insho;kirill;Город;\n"
        "ru3.txt;2016;referat;kirill;Реки;\n"
        "en1.pdf;2015;maqola;lotin;Village life;\n",
        encoding="utf-8",
    )
    return tmp_path


# ── script 1 ─────────────────────────────────────────────────────────────────

def test_mask_names():
    text = ("Bajardi: Karimov Aziz\nMuallif Rustamova Dilnoza Akmal qizi edi. "
            "Ivan Petrov keldi. A. Valiyev yozdi. Bog'da Lola o'ynadi.")
    out = prepare_human.mask_names(text, ["Lola"])
    for name in ("Karimov", "Aziz", "Rustamova", "Dilnoza", "Petrov", "Valiyev", "Lola"):
        assert name not in out, name
    assert "Bog'da" in out and "[ISM]" in out


def test_mask_email_phone():
    out = prepare_human.mask_names("Yozing: a.b@mail.uz yoki +998 90 123 45 67", [])
    assert "[EMAIL]" in out and "[TEL]" in out and "mail.uz" not in out


def test_strip_cover_finds_topic():
    body, topic = prepare_human.strip_cover(
        "TOSHKENT DAVLAT UNIVERSITETI\n\nReferat\n\nMavzu: Orol dengizi\n\n"
        "Bajardi: X\n\nOrol dengizi muammosi bugungi kunda juda dolzarb masala hisoblanadi."
    )
    assert topic == "Orol dengizi"
    assert body.startswith("Orol dengizi muammosi")


def test_prepare_end_to_end(data_dir):
    report = prepare_human.process(data_dir, 120, 350)
    rows = list(read_jsonl(data_dir / "work" / "human_chunks.jsonl"))
    assert rows and report["chunks"] == len(rows)
    assert {r["lang"] for r in rows} == {"uz", "ru", "en"}
    assert any(d["reason"] in {"exact_duplicate", "near_duplicate"} for d in report["dropped"])
    assert not any(r["source_file"] == "uz1_copy.txt" for r in rows)
    joined = " ".join(r["text"] for r in rows)
    assert "Karimov" not in joined and "aziz@example.com" not in joined
    assert "VAZIRLIGI" not in joined
    uz1 = [r for r in rows if r["source_file"] == "uz1.txt"]
    assert uz1 and all(r["topic"] == "Bahor fasli" for r in uz1)
    assert all(120 <= r["words"] <= 350 * 1.3 for r in rows)


# ── script 2 ─────────────────────────────────────────────────────────────────

class FakeGemini:
    def __init__(self, fail_first_with_429=False):
        self.prompts: list[str] = []
        self.fail = fail_first_with_429

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.fail:
            self.fail = False
            raise generate_ai.RateLimited(5)
        m = re.search(r"(\d+)", prompt)
        n = int(m.group(1)) if m else 150
        lang = "ru" if re.search(r"[а-я]", prompt) else "en"
        if "o'zbek" in prompt:
            lang = "uz"
        return make_text(lang, n, len(self.prompts) + 100)


class FakePacer(generate_ai.Pacer):
    def __init__(self):
        self.slept = []
        super().__init__(1000, clock=lambda: 0.0, sleep=self.slept.append)


def _windows(text: str, k: int = 6):
    toks = re.findall(r"\w+", text.lower())
    return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}


def test_generate_never_sends_human_text(data_dir):
    prepare_human.process(data_dir, 120, 350)
    fake = FakeGemini()
    generate_ai.generate(data_dir, key="k", model="m", rpm=1000, max_calls=1000,
                         call=fake, pacer=FakePacer(), log=lambda _m: None)
    human = list(read_jsonl(data_dir / "work" / "human_chunks.jsonl"))
    human_windows = set().union(*(_windows(h["text"]) for h in human))
    for p in fake.prompts:
        # translate-style second prompts carry the MODEL's first answer, which
        # is synthetic; nothing may match a 6-word window of a human chunk
        assert not (_windows(p) & human_windows)


def test_generate_is_resumable_and_respects_budget(data_dir):
    prepare_human.process(data_dir, 120, 350)
    chunks = list(read_jsonl(data_dir / "work" / "human_chunks.jsonl"))
    kw = dict(key="k", model="m", rpm=1000, pacer=FakePacer(), log=lambda _m: None)
    s1 = generate_ai.generate(data_dir, max_calls=3, call=FakeGemini(), **kw)
    assert s1["calls"] == 3 and 1 <= s1["written"] <= 3
    s2 = generate_ai.generate(data_dir, max_calls=1000, call=FakeGemini(), **kw)
    out = list(read_jsonl(data_dir / "work" / "ai_generated.jsonl"))
    assert len(out) == len(chunks)
    assert len({r["pair_id"] for r in out}) == len(out)            # no duplicates
    assert s2["done_before"] == s1["written"]


def test_generate_waits_on_429(data_dir):
    prepare_human.process(data_dir, 120, 350)
    pacer = FakePacer()
    generate_ai.generate(data_dir, key="k", model="m", rpm=1000, max_calls=1000,
                         call=FakeGemini(fail_first_with_429=True), pacer=pacer,
                         log=lambda _m: None)
    assert max(pacer.slept) >= 5


def test_prompt_styles():
    plain = generate_ai.build_prompt("plain", "uz", "insho", "Bahor", 200)
    assert len(plain) == 1 and "Bahor" in plain[0] and "200" in plain[0]
    two = generate_ai.build_prompt("translate", "uz", "insho", "Bahor", 200)
    assert len(two) == 2 and "tarjima" in two[1]
    assert "кирилл" not in generate_ai.build_prompt("plain", "uz-cyrl", "insho", "T", 100)[0]
    assert "kirill" in generate_ai.build_prompt("plain", "uz-cyrl", "insho", "T", 100)[0]
    assert {generate_ai.style_for(f"id{i}") for i in range(60)} == set(generate_ai.STYLES)


def test_retry_delay_parsing():
    body = {"error": {"details": [{"retryDelay": "17s"}]}}
    assert generate_ai._retry_delay(body) == 17.0


def test_manual_collection(data_dir):
    manual = data_dir / "ai_manual"
    manual.mkdir()
    (manual / "chatgpt_1.txt").write_text(make_text("uz", 300, 50), encoding="utf-8")
    (manual / "meta.csv").write_text("fayl;model;tur;mavzu\nchatgpt_1.txt;chatgpt;insho;Bahor\n",
                                     encoding="utf-8")
    assert generate_ai.collect_manual(data_dir) >= 1
    rows = list(read_jsonl(data_dir / "work" / "ai_manual.jsonl"))
    assert rows[0]["model"] == "chatgpt" and rows[0]["lang"] == "uz"


# ── script 3 + augmentation ──────────────────────────────────────────────────

def _full_pipeline(data_dir):
    prepare_human.process(data_dir, 120, 350)
    generate_ai.generate(data_dir, key="k", model="m", rpm=1000, max_calls=1000,
                         call=FakeGemini(), pacer=FakePacer(), log=lambda _m: None)
    return split.run(data_dir, 0.10, 0.10, 13)


def test_split_keeps_groups_together(data_dir):
    _full_pipeline(data_dir)
    sp = data_dir / "work" / "split"
    where = {}
    for name in ("train", "val", "test"):
        for r in read_jsonl(sp / f"{name}.jsonl"):
            assert where.setdefault(r["group"], name) == name
    assert "test" in where.values()
    test_rows = list(read_jsonl(sp / "test.jsonl"))
    assert {r["label"] for r in test_rows} == {0, 1}   # a human doc and its twins


def test_split_assignments_never_move(data_dir):
    _full_pipeline(data_dir)
    sp = data_dir / "work" / "split"
    before = json.loads((sp / "assignments.json").read_text(encoding="utf-8"))
    # new data arrives
    (data_dir / "human" / "uz9.txt").write_text(make_text("uz", 500, 99), encoding="utf-8")
    with (data_dir / "human" / "meta.csv").open("a", encoding="utf-8") as f:
        f.write("uz9.txt;2019;insho;lotin;Yangi mavzu;\n")
    _full_pipeline(data_dir)
    after = json.loads((sp / "assignments.json").read_text(encoding="utf-8"))
    assert all(after[g] == s for g, s in before.items())
    assert len(after) == len(before) + 1


def test_augment_balanced_and_noisy(data_dir):
    _full_pipeline(data_dir)
    r = augment_ocr.run(data_dir, fraction=0.5, intensity=0.2, seed=1)
    sp = data_dir / "work" / "split"
    train = list(read_jsonl(sp / "train.jsonl"))
    n_h = sum(1 for x in train if x["label"] == 0)
    n_a = sum(1 for x in train if x["label"] == 1)
    assert r["noisy_human"] == round(n_h * 0.5) and r["noisy_ai"] == round(n_a * 0.5)
    aug = list(read_jsonl(sp / "train_aug.jsonl"))
    assert len(aug) == len(train) + r["noisy_added"]
    noisy = [x for x in aug if x["id"].endswith("~ocr")]
    originals = {x["id"]: x["text"] for x in train}
    assert all(x["text"] != originals[x["id"][:-4]] for x in noisy)
    assert (sp / "test_ocr.jsonl").exists()


def test_noise_apostrophe_variants():
    rng = random.Random(3)
    outs = {augment_ocr.add_noise("o'qituvchi g'alaba qo'shiq", rng, 0.0) for _ in range(40)}
    assert len(outs) >= 4      # several o'/g' spellings, including none
    cyr = augment_ocr.add_noise("ўқувчи қизи ғалаба ҳовли " * 20, random.Random(1), 0.5)
    assert any(ch in cyr for ch in "укгх")


# ── evaluate ─────────────────────────────────────────────────────────────────

def test_auroc_known_values():
    assert evaluate.auroc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert evaluate.auroc([0, 1], [0.9, 0.1]) == 0.0
    assert evaluate.auroc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == 0.5
    assert evaluate.auroc([1, 1], [0.1, 0.2]) is None


def test_threshold_keeps_fpr_under_target():
    rng = random.Random(0)
    labels = [0] * 200 + [1] * 200
    scores = [rng.random() * 0.7 for _ in range(200)] + [0.3 + rng.random() * 0.7 for _ in range(200)]
    t = evaluate.choose_threshold(labels, scores, 0.05)
    assert evaluate.rates(labels, scores, t)["fpr"] <= 0.05
    # and it is the lowest such threshold: one notch lower breaks the target
    human = sorted((s for y, s in zip(labels, scores) if y == 0), reverse=True)
    assert evaluate.rates(labels, scores, human[10] - 1e-9)["fpr"] > 0.05


def test_evaluate_with_score_files(data_dir):
    _full_pipeline(data_dir)
    augment_ocr.run(data_dir, 0.3, 0.05, 1)
    sp = data_dir / "work" / "split"

    def write_scores(path, names):
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "score"])
            for name in names:
                for r in read_jsonl(sp / f"{name}.jsonl"):
                    w.writerow([r["id"], 0.9 if r["label"] else 0.1])

    write_scores(data_dir / "val.csv", ["val"])
    write_scores(data_dir / "test.csv", ["test", "test_ocr"])
    rep = evaluate.run(data_dir, model_dir=None, val_scores=data_dir / "val.csv",
                       test_scores=data_dir / "test.csv", max_fpr=0.05, write_threshold=False)
    assert rep["test"]["auroc"] == 1.0 and rep["test"]["fpr"] == 0.0
    assert "test_ocr" in rep
    with (data_dir / "work" / "eval" / "test_scores.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ["text_id", "haqiqiy", "bizning_ball", "quillbot_ball"]
    assert all(r["quillbot_ball"] == "" for r in rows)
    assert {r["haqiqiy"] for r in rows} == {"odam", "si"}
