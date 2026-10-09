"""Script 2: an AI-written twin for every human chunk, plus hand-collected AI texts.

    # needs DETECTOR_GEN_GEMINI_KEY (a FREE key — never the bot's key)
    python -m detector.training.generate_ai generate [--rpm 8 --max-calls 400]
    # no key needed: texts other models wrote, collected by hand
    python -m detector.training.generate_ai manual

PRIVACY: the human text itself is NEVER sent. A prompt carries only the
topic, genre, language and target length of its human twin (tests assert
this).

Three prompt styles, picked per chunk deterministically so a rerun asks the
same thing:
    plain      "write a <genre> about <topic>, ~N words"
    student    "write it the way a school/college student would"
    translate  write in another language first, then translate it — the
               "translated through" texts teachers see a lot of

RESUMABLE: every result is appended (fsynced) to work/ai_generated.jsonl; on
start, ids already there are skipped. Ctrl-C at any time is safe. Rate
limited to --rpm; on HTTP 429 it waits for the server's retryDelay (or backs
off exponentially) and continues; after --max-calls it stops cleanly.

Hand-collected AI texts: data/ai_manual/*.{txt,docx,pdf} with an optional
data/ai_manual/meta.csv (fayl, model, tur, mavzu) -> work/ai_manual.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from detector.service.lang import detect_language
from detector.service.text import clean_text, word_count
from detector.training.common import (
    SUPPORTED_SUFFIXES, ai_manual_dir, append_jsonl, read_document, read_jsonl,
    read_meta_csv, resolve_gen_key, stable_hash, work_dir, write_jsonl,
)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash"
STYLES = ("plain", "student", "translate")

LANG_NAME = {
    "uz": "o'zbek (lotin yozuvida)",
    "uz-cyrl": "o'zbek (kirill yozuvida)",
    "ru": "русском",
    "en": "English",
}

# Genre words as teachers write them in meta.csv -> how a prompt names them.
GENRE_NAME = {
    "uz": {"insho": "insho", "referat": "referat", "maqola": "maqola",
           "kurs ishi": "kurs ishi bo'limi", "xat": "xat", "kitob": "badiiy matn parchasi"},
    "ru": {"insho": "сочинение", "referat": "реферат", "maqola": "статью",
           "kurs ishi": "раздел курсовой работы", "xat": "письмо", "kitob": "отрывок художественного текста"},
    "en": {"insho": "essay", "referat": "report", "maqola": "article",
           "kurs ishi": "coursework section", "xat": "letter", "kitob": "fiction excerpt"},
}


def _genre(lang: str, genre: str) -> str:
    table = GENRE_NAME["uz" if lang.startswith("uz") else lang if lang in GENRE_NAME else "en"]
    return table.get(genre, genre if genre and genre != "unknown" else table["insho"])


def build_prompt(style: str, lang: str, genre: str, topic: str, words: int) -> list[str]:
    """The prompt(s) for one twin: one string, or two for "translate" (the
    second gets the first answer appended). Inputs are metadata only."""
    g = _genre(lang, genre)
    if lang.startswith("uz"):
        script = "kirill" if lang == "uz-cyrl" else "lotin"
        base = (f"\"{topic}\" mavzusida {g} yozing. Hajmi taxminan {words} so'z. "
                f"Faqat o'zbek tilida, {script} yozuvida yozing. Sarlavha, izoh va "
                f"ro'yxat qo'shmang, faqat matnning o'zini qaytaring.")
        student = (f"Siz o'quvchisiz. \"{topic}\" mavzusida {g} yozing, xuddi o'quvchi "
                   f"o'zi yozgandek: oddiy so'zlar, ba'zan takrorlar. Hajmi taxminan "
                   f"{words} so'z. O'zbek tilida, {script} yozuvida. Faqat matnni qaytaring.")
        via, translate = "ru", (f"Quyidagi matnni o'zbek tiliga ({script} yozuvida) tarjima "
                                f"qiling. Faqat tarjimani qaytaring.\n\n")
    elif lang == "ru":
        base = (f"Напишите {g} на тему «{topic}». Объём около {words} слов. Только на "
                f"русском языке, без заголовка и пояснений, только сам текст.")
        student = (f"Вы школьник. Напишите {g} на тему «{topic}» так, как написал бы "
                   f"ученик: простые слова, иногда повторы. Около {words} слов. Только текст.")
        via, translate = "en", "Переведите следующий текст на русский язык. Верните только перевод.\n\n"
    else:
        base = (f"Write a {g} on the topic \"{topic}\". About {words} words. No title, "
                f"no notes, return only the text.")
        student = (f"You are a school student. Write a {g} on \"{topic}\" the way a student "
                   f"would: simple words, some repetition. About {words} words. Only the text.")
        via, translate = "ru", "Translate the following text into English. Return only the translation.\n\n"

    if style == "plain":
        return [base]
    if style == "student":
        return [student]
    if style == "translate":
        first = build_prompt("plain", via, genre, topic, words)[0]
        return [first, translate]
    raise ValueError(style)


def style_for(chunk_id: str) -> str:
    return STYLES[stable_hash(chunk_id, "style") % len(STYLES)]


# ── HTTP ─────────────────────────────────────────────────────────────────────

class RateLimited(Exception):
    def __init__(self, wait: float) -> None:
        super().__init__(f"rate limited, wait {wait:.0f}s")
        self.wait = wait


class BudgetSpent(Exception):
    """--max-calls reached."""


class Blocked(Exception):
    """Safety block / empty answer: skip this chunk, do not retry forever."""


def _retry_delay(body: dict) -> float | None:
    for d in (body.get("error") or {}).get("details") or []:
        raw = d.get("retryDelay")
        if raw:
            m = re.match(r"([\d.]+)s", str(raw))
            if m:
                return float(m.group(1))
    return None


def gemini_call(prompt: str, *, key: str, model: str, client) -> str:
    resp = client.post(
        API_URL.format(model=model),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.9, "maxOutputTokens": 4096},
        },
        timeout=120,
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
    parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise Blocked(json.dumps(body.get("promptFeedback") or cands[:1])[:300])
    return text


class Pacer:
    """At most `rpm` calls per minute, evenly spaced."""

    def __init__(self, rpm: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.gap = 60.0 / rpm
        self.clock, self.sleep = clock, sleep
        self.next_at = 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self.next_at:
            self.sleep(self.next_at - now)
        self.next_at = max(now, self.next_at) + self.gap


def _strip_wrapping(text: str) -> str:
    text = re.sub(r"^```\w*\n|\n```$", "", text.strip())
    lines = text.split("\n")
    # A leading markdown title despite the instructions.
    if lines and (lines[0].startswith("#") or (lines[0].startswith("**") and lines[0].endswith("**"))):
        lines = lines[1:]
    return clean_text("\n".join(lines).replace("**", ""))


def generate(
    data_dir: Path | None, *, key: str, model: str, rpm: float, max_calls: int,
    call: Callable[[str], str] | None = None, pacer: Pacer | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    work = work_dir(data_dir)
    out_path = work / "ai_generated.jsonl"
    err_path = work / "generate_errors.jsonl"
    done = {r["pair_id"] for r in read_jsonl(out_path)}
    chunks = [c for c in read_jsonl(work / "human_chunks.jsonl")]
    todo = [c for c in chunks if c["id"] not in done]
    no_topic = [c["id"] for c in todo if not c.get("topic")]
    todo = [c for c in todo if c.get("topic")]
    if no_topic:
        log(f"{len(no_topic)} chunks have no topic (fill `mavzu` in meta.csv); skipped")

    if call is None:
        import httpx

        client = httpx.Client()
        call = lambda p: gemini_call(p, key=key, model=model, client=client)  # noqa: E731
    pacer = pacer or Pacer(rpm)

    stats = {"done_before": len(done), "written": 0, "calls": 0, "blocked": 0,
             "length_off": 0, "remaining": len(todo), "skipped_no_topic": len(no_topic)}

    def one_call(prompt: str) -> str:
        backoff = 30.0
        while True:
            if stats["calls"] >= max_calls:
                raise BudgetSpent
            pacer.wait()
            stats["calls"] += 1
            try:
                return call(prompt)
            except RateLimited as e:
                wait = max(e.wait, backoff)
                log(f"429: waiting {wait:.0f}s")
                pacer.sleep(wait)
                backoff = min(backoff * 2, 600.0)

    try:
        for c in todo:
            style = style_for(c["id"])
            target = int(c["words"])
            prompts = build_prompt(style, c["lang"], c.get("genre", ""), c["topic"], target)
            try:
                answer = one_call(prompts[0])
                if len(prompts) == 2:
                    answer = one_call(prompts[1] + answer)
            except Blocked as e:
                stats["blocked"] += 1
                append_jsonl(err_path, {"pair_id": c["id"], "error": "blocked", "detail": str(e)})
                continue
            text = _strip_wrapping(answer)
            n = word_count(text)
            length_ok = 0.65 * target <= n <= 1.35 * target
            if not length_ok:
                stats["length_off"] += 1
            got_lang = detect_language(text)
            append_jsonl(out_path, {
                "id": f"ai-{c['id']}",
                "pair_id": c["id"],
                "doc_id": c["doc_id"],
                "text": text,
                "lang": c["lang"],
                "detected_lang": got_lang,
                "genre": c.get("genre"),
                "topic": c["topic"],
                "style": style,
                "model": model,
                "words": n,
                "target_words": target,
                "length_ok": length_ok,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            stats["written"] += 1
            stats["remaining"] -= 1
            if stats["written"] % 10 == 0:
                log(f"{stats['written']} written, {stats['remaining']} left, {stats['calls']} calls")
    except BudgetSpent:
        log(f"--max-calls {max_calls} reached; rerun later to continue")
    except KeyboardInterrupt:
        log("interrupted; rerun to continue")
    return stats


def collect_manual(data_dir: Path | None) -> int:
    """data/ai_manual/* -> work/ai_manual.jsonl (label AI, no human twin)."""
    from detector.training.prepare_human import chunk_paragraphs

    src = ai_manual_dir(data_dir)
    meta = read_meta_csv(src / "meta.csv")
    rows = []
    if src.is_dir():
        for path in sorted(p for p in src.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES):
            m = meta.get(path.name, {})
            text = clean_text(read_document(path))
            lang = detect_language(text)
            doc_id = f"m{stable_hash(path.name):012x}"[:13]
            for k, chunk in enumerate(chunk_paragraphs(text, 120, 350) or [text]):
                rows.append({
                    "id": f"{doc_id}-{k:03d}", "pair_id": None, "doc_id": doc_id,
                    "text": chunk, "lang": lang, "genre": (m.get("tur") or "unknown").lower(),
                    "topic": m.get("mavzu") or None, "style": "manual",
                    "model": m.get("model") or "unknown", "words": word_count(chunk),
                    "source_file": path.name,
                })
    write_jsonl(work_dir(data_dir) / "ai_manual.jsonl", rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--data-dir", type=Path)
    g.add_argument("--model", default=os.environ.get("DETECTOR_GEN_GEMINI_MODEL", DEFAULT_MODEL))
    g.add_argument("--rpm", type=float, default=8.0,
                   help="calls per minute; keep under the free tier's limit")
    g.add_argument("--max-calls", type=int, default=400,
                   help="stop after this many calls (free tier has a daily cap)")
    g.add_argument("--env-file", type=Path,
                   help="read DETECTOR_GEN_GEMINI_KEY from this file (read only)")
    m = sub.add_parser("manual")
    m.add_argument("--data-dir", type=Path)
    args = ap.parse_args()

    if args.cmd == "manual":
        print(f"ai_manual.jsonl: {collect_manual(args.data_dir)} chunks")
        return
    key = resolve_gen_key(args.env_file)
    random.seed(0)
    stats = generate(args.data_dir, key=key, model=args.model, rpm=args.rpm,
                     max_calls=args.max_calls)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
