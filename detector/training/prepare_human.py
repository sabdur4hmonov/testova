"""Script 1: human texts -> clean, anonymised, chunked, de-duplicated JSONL.

    python -m detector.training.prepare_human [--min-words 120 --max-words 350]

Input: detector/data/human/*.{docx,txt,pdf} and data/human/meta.csv with
columns `fayl, yil, tur, yozuv` plus optional `mavzu` (topic) and `ismlar`
(names to mask, `|`-separated). See meta.example.csv.

Output: data/work/human_chunks.jsonl, one row per chunk:
    {id, doc_id, text, lang, genre, year, script, topic, words, source_file}
and data/work/prepare_report.json (what was dropped and why).

Name masking is heuristic (there is no free Uzbek NER model): names listed in
meta.csv, the rest of "Bajardi:/Выполнил:"-style lines, patronymic patterns
("... o'g'li", "... qizi", "...ovich") and Uzbek/Russian surname shapes
("Karimov", "Karimova") next to another capitalised word. It will miss some.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from detector.service.lang import UNKNOWN, detect_language
from detector.service.text import clean_text, word_count
from detector.training.common import (
    SUPPORTED_SUFFIXES, human_dir, read_document, read_meta_csv, stable_hash,
    work_dir, write_jsonl,
)

MASK = "[ISM]"
_APOS = "['ʻʼ‘’`]"

# Lines whose remainder is a person: author, supervisor, checker.
_ROLE_LINE = re.compile(
    r"^\s*(?:bajardi|bajaruvchi|tayyorladi|tayyorlagan|muallif|talaba|o"
    + _APOS + r"quvchi|ilmiy\s+rahbar|rahbar|tekshirdi|qabul\s+qildi|f\.?\s*i\.?\s*sh\.?|"
    r"бажарди|тайёрлади|муаллиф|талаба|ўқувчи|раҳбар|текширди|"
    r"выполнил[аи]?|подготовил[аи]?|автор|студент(?:ка)?|учени(?:к|ца)|"
    r"руководитель|научный\s+руководитель|проверил[аи]?|ф\.?\s*и\.?\s*о\.?)"
    r"(\s*[:\-–—]\s*|\s+)(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_CAP = r"[A-ZА-ЯЁЎҚҒҲ][a-zа-яёўқғҳ" + _APOS[1:-1] + r"]+"
_PATRONYMIC = re.compile(
    rf"(?:{_CAP}\s+){{1,2}}{_CAP}?\s*(?:o{_APOS}?g{_APOS}?li|qizi|ўғли|қизи|"
    rf"[a-zа-я]+(?:ovich|evich|ovna|evna|ович|евич|овна|евна))\b"
)
_SURNAME = re.compile(
    rf"\b{_CAP}(?:ov|ova|ev|eva|yev|yeva|ova|ов|ова|ев|ева|ин|ина)\b"
)
_SURNAME_WITH_NAME = re.compile(
    rf"(?:\b{_CAP}\s+{_SURNAME.pattern}|{_SURNAME.pattern}\s+{_CAP}\b|"
    rf"\b[A-ZА-ЯЎҚҒҲ]\.\s*(?:[A-ZА-ЯЎҚҒҲ]\.\s*)?{_SURNAME.pattern})"
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PHONE = re.compile(r"(?:\+?998|8)?[\s(-]*\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b")

# Cover-page lines: ministry/university headers in caps, "Referat", "Mavzu:".
_COVER_LINE = re.compile(
    r"^\s*(?:mavzu|тема|мавзу|referat|реферат|kurs\s+ishi|курсовая|курс\s+иши|"
    r"mustaqil\s+ish|мустақил\s+иш)\s*[:.]?\s*",
    re.IGNORECASE,
)


def mask_names(text: str, extra_names: list[str]) -> str:
    # Case-sensitive, whole words: "Aziz" is a name, "aziz" ("dear") is not.
    for name in sorted({n.strip() for n in extra_names if n.strip()}, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", MASK, text)

    def _role(m: re.Match) -> str:
        # "Bajardi: X Y" or a short "Bajardi X Y" line is a signature; a
        # sentence that merely starts with "Muallif ..." is not.
        if ":" in m.group(1) or "-" in m.group(1) or "–" in m.group(1) or "—" in m.group(1)                 or len(m.group(2).split()) <= 4:
            return m.group(0)[: m.start(2) - m.start(0)] + MASK
        return m.group(0)

    text = _ROLE_LINE.sub(_role, text)
    text = _PATRONYMIC.sub(MASK, text)
    text = _SURNAME_WITH_NAME.sub(MASK, text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[TEL]", text)
    return re.sub(rf"(?:{re.escape(MASK)}[\s,]*){{2,}}", MASK + " ", text)


def strip_cover(text: str) -> tuple[str, str | None]:
    """Drop the cover-page block at the top (all-caps headers, role lines,
    "Mavzu: ..."), returning (body, topic found on the cover or None)."""
    topic = None
    paras = text.split("\n\n")
    body_start = 0
    for i, para in enumerate(paras[:15]):
        p = para.strip()
        m = _COVER_LINE.match(p)
        if m:
            rest = p[m.end():].strip().strip('"«»“”')
            if rest and not topic and len(rest.split()) <= 15:
                topic = rest
            body_start = i + 1
            continue
        letters = [c for c in p if c.isalpha()]
        is_caps = letters and sum(c.isupper() for c in letters) / len(letters) > 0.8
        if is_caps or _ROLE_LINE.match(p) or (len(p.split()) <= 4 and not p.endswith(".")):
            body_start = i + 1
            continue
        break
    return "\n\n".join(paras[body_start:]), topic


def first_heading(text: str) -> str | None:
    first = text.strip().split("\n", 1)[0].strip()
    if 2 <= len(first.split()) <= 12 and not first.endswith("."):
        return first
    return None


def chunk_paragraphs(text: str, min_words: int, max_words: int) -> list[str]:
    """Greedy paragraph packing. A paragraph longer than max_words is split on
    sentence ends; a final remainder under min_words joins the previous chunk
    when that keeps it under 1.3 x max_words, else it is dropped."""
    from detector.service.text import split_sentences

    units: list[str] = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if word_count(para) <= max_words:
            units.append(para)
        else:
            buf: list[str] = []
            for s in split_sentences(para):
                if buf and word_count(" ".join(buf + [s])) > max_words:
                    units.append(" ".join(buf))
                    buf = []
                buf.append(s)
            if buf:
                units.append(" ".join(buf))
    chunks: list[str] = []
    cur: list[str] = []
    for u in units:
        if cur and word_count("\n\n".join(cur + [u])) > max_words:
            chunks.append("\n\n".join(cur))
            cur = []
        cur.append(u)
    if cur:
        tail = "\n\n".join(cur)
        if word_count(tail) >= min_words:
            chunks.append(tail)
        elif chunks and word_count(chunks[-1]) + word_count(tail) <= max_words * 1.3:
            chunks[-1] = chunks[-1] + "\n\n" + tail
    return [c for c in chunks if word_count(c) >= min_words]


# ── near-duplicate detection (MinHash + LSH) ────────────────────────────────

_NUM_PERM = 64
_BANDS = 16          # 16 bands x 4 rows: pairs above ~0.6 Jaccard collide
_MASK64 = (1 << 61) - 1


def _shingles(text: str, k: int = 5) -> set[str]:
    toks = re.findall(r"\w+", text.lower())
    return {" ".join(toks[i:i + k]) for i in range(max(1, len(toks) - k + 1))}


def _minhash(sh: set[str]) -> list[int]:
    hs = [stable_hash(s) for s in sh]
    sig = []
    for i in range(_NUM_PERM):
        a, b = 2 * i + 1, 7919 * (i + 1)
        sig.append(min(((a * h + b) & _MASK64) for h in hs))
    return sig


def dedupe(rows: list[dict], threshold: float = 0.8) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). Exact duplicates by normalised text; near duplicates by
    5-word-shingle Jaccard >= threshold (candidates from LSH, then verified)."""
    kept: list[dict] = []
    dropped: list[dict] = []
    seen_exact: set[str] = set()
    buckets: dict[tuple, list[int]] = {}
    shingles: list[set[str]] = []
    rows_per_band = _NUM_PERM // _BANDS
    for row in rows:
        norm = re.sub(r"\W+", " ", row["text"].lower()).strip()
        if norm in seen_exact:
            dropped.append({"id": row["id"], "reason": "exact_duplicate"})
            continue
        sh = _shingles(row["text"])
        sig = _minhash(sh)
        keys = [(b, tuple(sig[b * rows_per_band:(b + 1) * rows_per_band])) for b in range(_BANDS)]
        dup_of = None
        for key in keys:
            for idx in buckets.get(key, []):
                other = shingles[idx]
                jac = len(sh & other) / max(1, len(sh | other))
                if jac >= threshold:
                    dup_of = kept[idx]["id"]
                    break
            if dup_of:
                break
        if dup_of:
            dropped.append({"id": row["id"], "reason": "near_duplicate", "of": dup_of})
            continue
        seen_exact.add(norm)
        idx = len(kept)
        kept.append(row)
        shingles.append(sh)
        for key in keys:
            buckets.setdefault(key, []).append(idx)
    return kept, dropped


def process(data_dir: Path | None, min_words: int, max_words: int) -> dict:
    src = human_dir(data_dir)
    # meta.csv plus any meta_*.csv (e.g. meta_telegram.csv from
    # transcribe_telegram build); a file listed twice keeps its first row.
    meta: dict = {}
    for mpath in [src / "meta.csv", *sorted(src.glob("meta_*.csv"))]:
        for name, row in read_meta_csv(mpath).items():
            meta.setdefault(name, row)
    rows: list[dict] = []
    report: dict = {"files": 0, "skipped_files": [], "chunks_before_dedupe": 0}
    for path in sorted(p for p in src.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES):
        report["files"] += 1
        m = meta.get(path.name, {})
        if not m:
            report["skipped_files"].append({"file": path.name, "reason": "not in meta.csv"})
        try:
            raw = read_document(path)
        except Exception as e:  # noqa: BLE001 — one broken file must not stop the run
            report["skipped_files"].append({"file": path.name, "reason": f"read error: {e}"})
            continue
        text = clean_text(raw)
        body, cover_topic = strip_cover(text)
        names = (m.get("ismlar") or "").split("|")
        body = mask_names(body, names)
        lang = detect_language(body)
        if lang == UNKNOWN:
            report["skipped_files"].append({"file": path.name, "reason": "language unknown"})
            continue
        topic = m.get("mavzu") or cover_topic or first_heading(body)
        doc_id = f"h{stable_hash(path.name):012x}"[:13]
        for k, chunk in enumerate(chunk_paragraphs(body, min_words, max_words)):
            rows.append({
                "id": f"{doc_id}-{k:03d}",
                "doc_id": doc_id,
                "text": chunk,
                "lang": lang,
                "genre": (m.get("tur") or "unknown").lower(),
                "year": m.get("yil") or None,
                "script": (m.get("yozuv") or "").lower() or None,
                "topic": topic,
                "words": word_count(chunk),
                "source_file": path.name,
                # "toza" (written before public ChatGPT) | "tasdiqlanmagan" | None
                "status": (m.get("holat") or "").lower() or None,
                "posted": m.get("sana") or None,
            })
    report["chunks_before_dedupe"] = len(rows)
    kept, dropped = dedupe(rows)
    report["chunks"] = len(kept)
    report["dropped"] = dropped
    report["by_lang"] = dict(Counter(r["lang"] for r in kept))
    report["by_genre"] = dict(Counter(r["genre"] for r in kept))
    report["by_status"] = dict(Counter(r["status"] or "unlabelled" for r in kept))
    report["without_topic"] = sum(1 for r in kept if not r["topic"])
    out = work_dir(data_dir)
    write_jsonl(out / "human_chunks.jsonl", kept)
    (out / "prepare_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--min-words", type=int, default=120)
    ap.add_argument("--max-words", type=int, default=350)
    args = ap.parse_args()
    report = process(args.data_dir, args.min_words, args.max_words)
    print(json.dumps({k: v for k, v in report.items() if k != "dropped"},
                     ensure_ascii=False, indent=2))
    print(f"dropped as duplicates: {len(report['dropped'])}")


if __name__ == "__main__":
    main()
