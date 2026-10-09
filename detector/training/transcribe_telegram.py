"""Handwritten Telegram essays (photos) -> verbatim text, for the A2 pipeline.

    # needs DETECTOR_GEN_GEMINI_KEY — the FREE key, never the bot's key.
    python -m detector.training.transcribe_telegram transcribe [--rpm 4 --max-calls 200]
    python -m detector.training.transcribe_telegram build      # filters, names, dedupe, labels
    python -m detector.training.transcribe_telegram review     # 20 random: photos | text, HTML

INPUT  data/human/telegram_albums.json (+ telegram_images/), written by the
       collection step. One CANONICAL album = one essay, pages in page_index
       order; re-posted albums (canonical_album != id) are skipped.
CACHE  data/work/transcripts/<album>.json — one file per album, written as soon
       as its answer arrives. A rerun skips albums already cached, so a
       crash, Ctrl-C, a 429 or the daily free-tier cap loses nothing.
OUTPUT (build) data/human/tg_essay_<album>.txt + data/human/meta_telegram.csv,
       which prepare_human reads like any other human text, and
       data/work/telegram_build_report.json.

The prompt is a transcription order, nothing else: copy word for word, keep
every spelling and grammar mistake, add nothing, mark what cannot be read as
[?], and return the assignment text and personal names SEPARATELY from the
essay so they never reach training as essay text.

"toza" / "tasdiqlanmagan": an essay posted before 2022-11-01 (before public
ChatGPT) is "toza"; later ones are "tasdiqlanmagan" — they may be human, but
nothing proves it. split.py lets only "toza" essays into the test set.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import random
import re
import sys

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from detector.service.text import clean_text, word_count
from detector.training.common import human_dir, resolve_gen_key, work_dir
from detector.training.generate_ai import (
    API_URL, Blocked, BudgetSpent, Pacer, RateLimited, _retry_delay,
)

DEFAULT_MODEL = "gemini-2.5-flash"
CLEAN_BEFORE = "2022-11-01"          # public ChatGPT release: 2022-11-30
MIN_WORDS = 150
MAX_UNREADABLE_SHARE = 0.03          # more than 3% [?] -> too unreliable to train on
UNREADABLE = "[?]"

PROMPT = """These photos are the pages, in order, of ONE handwritten school essay in Uzbek.

Transcribe it EXACTLY as written:
- copy every word as the writer wrote it; do NOT correct spelling, grammar,
  punctuation or word order; keep the writer's mistakes;
- do NOT add, complete, summarize, translate or explain anything;
- write a word or a passage you cannot read as [?] (one [?] per unreadable word);
- keep paragraph breaks as blank lines; join a word split across pages/lines.

Separate three things:
- "task": the assignment/question text if it is written or printed on the pages
  (otherwise ""), word for word;
- "names": every personal name or signature (student, teacher, channel) that
  appears, as written;
- "essay": the essay body only, WITHOUT the task text and WITHOUT the names.

Reply with JSON only:
{"task": "...", "names": ["..."], "essay": "...", "pages_read": <number of pages>}"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "task": {"type": "STRING"},
        "names": {"type": "ARRAY", "items": {"type": "STRING"}},
        "essay": {"type": "STRING"},
        "pages_read": {"type": "INTEGER"},
    },
    "required": ["task", "names", "essay"],
}


# ── albums ──────────────────────────────────────────────────────────────────

def load_albums(data_dir: Path | None = None) -> list[dict]:
    """Canonical albums only, pages sorted by page_index."""
    src = human_dir(data_dir)
    albums = json.loads((src / "telegram_albums.json").read_text(encoding="utf-8"))
    out = []
    for a in albums:
        if a.get("canonical_album", a["id"]) != a["id"]:
            continue
        pages = sorted(a.get("pages") or [], key=lambda p: p.get("page_index", 0))
        out.append({**a, "pages": pages})
    return out


def status_for(posted_at: str | None) -> str:
    return "toza" if posted_at and posted_at[:10] < CLEAN_BEFORE else "tasdiqlanmagan"


# ── Gemini Vision ───────────────────────────────────────────────────────────

def vision_call(images: list[bytes], *, key: str, model: str, client) -> dict:
    parts = [{"text": PROMPT}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode("ascii")}}
        for b in images
    ]
    resp = client.post(
        API_URL.format(model=model),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.0, "maxOutputTokens": 8192,
                "responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA,
            },
        },
        timeout=180,
    )
    if resp.status_code == 429:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        raise RateLimited(_retry_delay(body) or 60.0)
    if resp.status_code >= 500:
        raise RateLimited(30.0)
    resp.raise_for_status()
    body = resp.json()
    cands = body.get("candidates") or []
    parts_out = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
    text = "".join(p.get("text", "") for p in parts_out).strip()
    if not text:
        raise Blocked(json.dumps(body.get("promptFeedback") or cands[:1])[:300])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise Blocked(f"not JSON: {text[:200]}") from e
    usage = body.get("usageMetadata") or {}
    data["_usage"] = {k: usage.get(k) for k in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")}
    return data


def transcribe(data_dir: Path | None, *, key: str, model: str, rpm: float, max_calls: int,
               call: Callable[[list[bytes]], dict] | None = None, pacer: Pacer | None = None,
               log: Callable[[str], None] = print) -> dict:
    src = human_dir(data_dir)
    cache = work_dir(data_dir) / "transcripts"
    cache.mkdir(parents=True, exist_ok=True)
    albums = load_albums(data_dir)
    todo = [a for a in albums if not (cache / f"{a['id']}.json").exists()]
    if call is None:
        import httpx

        client = httpx.Client()
        call = lambda imgs: vision_call(imgs, key=key, model=model, client=client)  # noqa: E731
    pacer = pacer or Pacer(rpm)
    stats = {"albums": len(albums), "cached_before": len(albums) - len(todo), "written": 0,
             "calls": 0, "blocked": 0, "remaining": len(todo)}

    def one(images: list[bytes]) -> dict:
        backoff = 30.0
        while True:
            if stats["calls"] >= max_calls:
                raise BudgetSpent
            pacer.wait()
            stats["calls"] += 1
            try:
                return call(images)
            except RateLimited as e:
                wait = max(e.wait, backoff)
                log(f"429: waiting {wait:.0f}s")
                pacer.sleep(wait)
                backoff = min(backoff * 2, 900.0)

    try:
        for a in todo:
            images = [(src / p["file"]).read_bytes() for p in a["pages"]]
            try:
                out = one(images)
            except Blocked as e:
                stats["blocked"] += 1
                (cache / f"{a['id']}.error.json").write_text(
                    json.dumps({"album": a["id"], "error": str(e)}, ensure_ascii=False), encoding="utf-8")
                continue
            record = {
                "album": a["id"], "pages": [p["file"] for p in a["pages"]], "model": model,
                "task": out.get("task") or "", "names": out.get("names") or [],
                "essay": out.get("essay") or "", "pages_read": out.get("pages_read"),
                "usage": out.get("_usage"),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            tmp = cache / f"{a['id']}.json.tmp"
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(cache / f"{a['id']}.json")       # atomic: never a half file
            stats["written"] += 1
            stats["remaining"] -= 1
            if stats["written"] % 10 == 0:
                log(f"{stats['written']} transcribed, {stats['remaining']} left")
    except BudgetSpent:
        log(f"--max-calls {max_calls} reached; rerun later to continue")
    except KeyboardInterrupt:
        log("interrupted; rerun to continue")
    return stats


# ── build: filter, mask, dedupe, label ──────────────────────────────────────

def unreadable_share(text: str) -> float:
    unk = text.count(UNREADABLE)
    words = word_count(text.replace(UNREADABLE, " "))
    return unk / max(1, words + unk)


def build(data_dir: Path | None, *, min_words: int = MIN_WORDS,
          max_unreadable: float = MAX_UNREADABLE_SHARE) -> dict:
    from detector.training.prepare_human import dedupe, mask_names

    src = human_dir(data_dir)
    cache = work_dir(data_dir) / "transcripts"
    albums = {a["id"]: a for a in load_albums(data_dir)}
    report = {"albums": len(albums), "transcribed": 0, "rejected": [], "kept": 0,
              "toza": 0, "tasdiqlanmagan": 0}
    candidates = []
    for aid, a in sorted(albums.items()):
        path = cache / f"{aid}.json"
        if not path.exists():
            continue
        report["transcribed"] += 1
        rec = json.loads(path.read_text(encoding="utf-8"))
        essay = clean_text(rec.get("essay") or "")
        names = [n for n in rec.get("names") or [] if isinstance(n, str) and n.strip()]
        # names the model failed to separate are still masked in the body
        essay = mask_names(essay, names + [w for n in names for w in n.split() if len(w) > 2])
        task = clean_text(rec.get("task") or "")
        if task and task in essay:
            essay = clean_text(essay.replace(task, ""))
        n_words = word_count(essay.replace(UNREADABLE, " "))
        share = unreadable_share(essay)
        reason = None
        if not a.get("complete_download", True):
            reason = "incomplete_album"
        elif n_words < min_words:
            reason = f"too_short:{n_words}"
        elif share > max_unreadable:
            reason = f"unreadable:{share:.3f}"
        if reason:
            report["rejected"].append({"album": aid, "reason": reason})
            continue
        candidates.append({"id": f"tg{aid}", "album": aid, "text": essay, "task": task,
                           "words": n_words, "unreadable": round(share, 4)})
    kept, dropped = dedupe(candidates)
    for d in dropped:
        report["rejected"].append({"album": d["id"], "reason": d["reason"], "of": d.get("of")})

    # Write txt + meta; old outputs of a previous build are replaced.
    for old in src.glob("tg_essay_*.txt"):
        old.unlink()
    meta_rows = ["fayl;yil;tur;yozuv;mavzu;ismlar;holat;sana;manba"]
    for c in kept:
        a = albums[c["album"]]
        name = f"tg_essay_{c['album']:05d}.txt"
        (src / name).write_text(c["text"] + "\n", encoding="utf-8")
        status = status_for(a.get("posted_at"))
        report[status] += 1
        topic = (a.get("topic") or "").replace(";", ",")
        meta_rows.append(";".join([name, "", "insho", a.get("script") or "lotin", topic, "",
                                   status, (a.get("posted_at") or "")[:10], a.get("source") or ""]))
    (src / "meta_telegram.csv").write_text("\n".join(meta_rows) + "\n", encoding="utf-8")
    report["kept"] = len(kept)
    report["tasks_separated"] = sum(1 for c in kept if c["task"])
    (work_dir(data_dir) / "telegram_build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


# ── review page ─────────────────────────────────────────────────────────────

def review(data_dir: Path | None, n: int = 20, seed: int = 7) -> Path:
    """data/work/telegram_review.html: n random kept essays, photos beside text."""
    src = human_dir(data_dir)
    work = work_dir(data_dir)
    albums = {a["id"]: a for a in load_albums(data_dir)}
    kept = sorted(int(p.stem.split("_")[-1]) for p in src.glob("tg_essay_*.txt"))
    pick = random.Random(seed).sample(kept, min(n, len(kept)))
    cards = []
    for aid in pick:
        a = albums[aid]
        rec = json.loads((work / "transcripts" / f"{aid}.json").read_text(encoding="utf-8"))
        text = (src / f"tg_essay_{aid:05d}.txt").read_text(encoding="utf-8")
        body = html.escape(text).replace(html.escape(UNREADABLE), '<mark>[?]</mark>')
        imgs = "".join(
            f'<img src="../human/{html.escape(p["file"])}" alt="sahifa {p["page_index"]}" loading="lazy">'
            for p in a["pages"])
        cards.append(f"""<section class="card">
<h2>Albom {aid} · {html.escape(a.get('topic') or '')} · {html.escape((a.get('posted_at') or '')[:10])}
 · {status_for(a.get('posted_at'))} · {word_count(text)} so'z</h2>
<div class="pair"><div class="imgs">{imgs}</div>
<div class="text"><p class="meta">Topshiriq (ajratilgan): {html.escape(rec.get('task') or '—')}<br>
Ismlar (ajratilgan, niqoblangan): {html.escape(', '.join(rec.get('names') or []) or '—')}</p>
<pre>{body}</pre></div></div></section>""")
    page = f"""<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transkripsiya tekshiruvi</title><style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#1b1f24; --muted:#5b6672; --line:#d8dee4; --mark:#ffe08a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1318; --card:#181e25; --text:#e6edf3; --muted:#9aa6b2; --line:#2c3540; --mark:#6b5a12; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,sans-serif; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; margin:0 auto 18px; max-width:1400px; }}
h2 {{ font-size:15px; margin:0 0 10px; }} .meta {{ color:var(--muted); font-size:13px; }}
.pair {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
@media (max-width: 800px) {{ .pair {{ grid-template-columns:1fr; }} }}
.imgs img {{ width:100%; height:auto; border:1px solid var(--line); border-radius:8px; margin-bottom:8px; }}
pre {{ white-space:pre-wrap; font:inherit; margin:0; }} mark {{ background:var(--mark); color:inherit; }}
</style></head><body>
<h1 style="font-size:18px;max-width:1400px;margin:0 auto 12px">Transkripsiya tekshiruvi — tasodifiy {len(pick)} ta insho (urug' {seed})</h1>
{''.join(cards)}
</body></html>"""
    out = work / "telegram_review.html"
    out.write_text(page, encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("--data-dir", type=Path)
    t.add_argument("--model", default=os.environ.get("DETECTOR_GEN_GEMINI_MODEL", DEFAULT_MODEL))
    t.add_argument("--rpm", type=float, default=4.0, help="image calls per minute (free tier)")
    t.add_argument("--max-calls", type=int, default=200)
    t.add_argument("--env-file", type=Path, help="read DETECTOR_GEN_GEMINI_KEY from this file (read only)")
    b = sub.add_parser("build")
    b.add_argument("--data-dir", type=Path)
    b.add_argument("--min-words", type=int, default=MIN_WORDS)
    b.add_argument("--max-unreadable", type=float, default=MAX_UNREADABLE_SHARE)
    r = sub.add_parser("review")
    r.add_argument("--data-dir", type=Path)
    r.add_argument("-n", type=int, default=20)
    args = ap.parse_args()

    if args.cmd == "transcribe":
        key = resolve_gen_key(args.env_file)
        print(json.dumps(transcribe(args.data_dir, key=key, model=args.model, rpm=args.rpm,
                                    max_calls=args.max_calls), indent=2))
    elif args.cmd == "build":
        rep = build(args.data_dir, min_words=args.min_words, max_unreadable=args.max_unreadable)
        print(json.dumps({k: v for k, v in rep.items() if k != "rejected"}, indent=2))
        print(f"rejected: {len(rep['rejected'])}")
    else:
        print(review(args.data_dir, n=args.n))


if __name__ == "__main__":
    main()
