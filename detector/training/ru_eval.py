"""Russian detector candidates on open Russian data (task 5, 2026-10-05).

    detector/.venv/Scripts/python -m detector.training.ru_eval data      # build the sets
    detector/.venv/Scripts/python -m detector.training.ru_eval run       # score candidates

SETS (data/ru_eval/, git-ignored), each 100 human + 100 machine, Russian only,
drawn with a fixed seed. Every set has a CALIBRATION half from the dataset's
validation split and a TEST half from its test split:

  coat      RussianNLP/coat (Apache-2.0) — the expanded RuATD corpus; both halves
            come from its validation split (its test labels are hidden). Older
            generators (ruGPT-3, mT5, OPUS-MT, ...) and short texts; nobody's
            training data among the candidates we can use.
  llmtrace  iitolstykh/LLMTrace_classification (Apache-2.0) — modern LLMs
            (GPT-4o, Gemini, ...), several domains. The toiletsandpaper
            models were trained on its TRAIN split, so this is in-domain for
            them (their test split is not their training data, but it is the
            same distribution) — read their numbers with that in mind.

METRICS per candidate and set: AUROC; accuracy, FPR and TPR at the model's
own 0.5 cut; and at a threshold chosen on the calibration half so that at most
5% of human texts there are flagged (the threshold the service would use),
measured on the test half — the honest number.

The LLMTrace jsonl files are 300 MB each; they are streamed once and
reservoir-sampled, nothing large is stored.
"""
from __future__ import annotations

import argparse
import io
import json
import random
import time
from pathlib import Path

from detector.training.common import DATA_DIR, read_jsonl, write_jsonl
from detector.training.evaluate import auroc, choose_threshold, rates

OUT = DATA_DIR / "ru_eval"
N_PER_LABEL = 100
SEED = 2026
MIN_WORDS = 30          # CoAT is short; below this a detector has nothing to read

HF = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

# (repo id, license, how to read the AI probability)
CANDIDATES = [
    ("toiletsandpaper/russian-ai-text-detector-modernbert", "apache-2.0", "ai_label"),
    ("toiletsandpaper/russian-ai-text-detector-bert", "MIT", "ai_label"),
    ("toiletsandpaper/russian-ai-text-detector-mini-frida", "MIT", "ai_label"),
    ("daalft/xlmr-ai-text-detection", "MIT", "index1"),
    ("bibbbu/multilingual-ai-human-detector_xlm-roberta-base", "MIT", "index1"),
    ("Ramez248/xlm-roberta-ai-text-detection", "apache-2.0", "index1"),
    ("trokhymovych/mbert-ai-bot-detector", "apache-2.0", "index1"),
]


def _words(t: str) -> int:
    return len(t.split())


# ── data ────────────────────────────────────────────────────────────────────

def _coat(rng: random.Random) -> tuple[list[dict], list[dict]]:
    """(calibration, test) — both from CoAT's VALIDATION split, disjoint: the
    published test split has no labels (they are the Kaggle leaderboard's)."""
    import httpx
    import pyarrow.parquet as pq

    url = HF.format(repo="RussianNLP/coat", path="binary/validation-00000-of-00001.parquet")
    data = httpx.get(url, follow_redirects=True, timeout=300).content
    table = pq.read_table(io.BytesIO(data)).to_pylist()
    pools = {0: [], 1: []}
    for r in table:
        if r.get("label") in (0, 1) and _words(r["text"]) >= MIN_WORDS:
            pools[r["label"]].append(r)
    calib, test = [], []
    for label, pool in pools.items():
        picked = rng.sample(pool, min(2 * N_PER_LABEL, len(pool)))
        for k, r in enumerate(picked):
            row = {"id": f"coat-{r['id']}", "text": r["text"], "label": label, "source": "coat"}
            (calib if k < N_PER_LABEL else test).append(row)
    return calib, test


def iter_lines(chunks):
    """Lines of a byte stream, split on the newline byte ONLY (httpx.iter_lines
    also splits on CR and Unicode separators that can sit inside a JSON
    string). Only the unfinished tail is kept between chunks, so memory stays
    at one line whatever the file size."""
    tail = bytearray()
    for chunk in chunks:
        start = 0
        while (nl := chunk.find(b"\n", start)) != -1:
            tail += chunk[start:nl]
            yield bytes(tail)
            tail.clear()
            start = nl + 1
        tail += chunk[start:]
        if len(tail) > 50_000_000:
            raise ValueError("a single jsonl line over 50 MB")
    if tail:
        yield bytes(tail)

def _llmtrace(split: str, rng: random.Random) -> list[dict]:
    """Stream the jsonl once; reservoir-sample Russian human/ai texts."""
    import httpx

    url = HF.format(repo="iitolstykh/LLMTrace_classification", path=f"{split}.jsonl")
    res = {0: [], 1: []}
    seen = {0: 0, 1: 0}
    with httpx.stream("GET", url, follow_redirects=True, timeout=600) as resp:
        for line in iter_lines(resp.iter_raw()):
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("lang") != "ru" or _words(r.get("text") or "") < MIN_WORDS:
                continue
            label = 1 if r.get("label") == "ai" else 0
            seen[label] += 1
            item = {"id": f"llmtrace-{split}-{r.get('topic_id')}-{seen[label]}", "text": r["text"],
                    "label": label, "source": "llmtrace", "domain": r.get("data_type"),
                    "model": r.get("model")}
            if len(res[label]) < N_PER_LABEL:
                res[label].append(item)
            else:
                j = rng.randrange(seen[label])
                if j < N_PER_LABEL:
                    res[label][j] = item
    return res[0] + res[1]


def build_sets() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    coat_calib, coat_test = _coat(random.Random(f"{SEED}-coat"))
    built = {
        "coat_calib": coat_calib, "coat_test": coat_test,
        "llmtrace_calib": _llmtrace("valid", random.Random(f"{SEED}-llmtrace-calib")),
        "llmtrace_test": _llmtrace("test", random.Random(f"{SEED}-llmtrace-test")),
    }
    for key, rows in built.items():

        write_jsonl(OUT / f"{key}.jsonl", rows)
        summary[key] = {
            "n": len(rows), "human": sum(r["label"] == 0 for r in rows),
            "ai": sum(r["label"] == 1 for r in rows),
            "median_words": sorted(_words(r["text"]) for r in rows)[len(rows) // 2] if rows else 0,
        }
    (OUT / "sets.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# ── scoring ─────────────────────────────────────────────────────────────────

def load_candidate(repo: str):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(repo)
    # bf16 + low_cpu_mem_usage: an xlm-roberta-base checkpoint is 1.1 GB in
    # fp32, more than this laptop has free; bf16 halves it. Scores differ from
    # fp32 in the third decimal at most — irrelevant for AUROC/FPR here.
    model = AutoModelForSequenceClassification.from_pretrained(
        repo, torch_dtype=torch.bfloat16, low_cpu_mem_usage=True).eval()
    torch.set_num_threads(4)
    return tok, model


def ai_index(model, rule: str) -> int:
    labels = {int(k): str(v).lower() for k, v in (model.config.id2label or {}).items()}
    if rule == "ai_label":
        for i, name in labels.items():
            if name in ("ai", "generated", "machine", "fake", "bot"):
                return i
    return 1      # LABEL_1 = AI / bot / generated for every index1 candidate (model cards)


def score(tok, model, idx: int, texts: list[str], batch: int = 4, max_len: int = 512) -> list[float]:
    import torch

    out = []
    for i in range(0, len(texts), batch):
        enc = tok(texts[i:i + batch], truncation=True, max_length=max_len, padding=True,
                  return_tensors="pt")
        with torch.no_grad():
            logits = model(**enc).logits
        logits = logits.float()
        if logits.shape[-1] == 1:
            p = torch.sigmoid(logits[:, 0])
        else:
            p = torch.softmax(logits, -1)[:, idx]
        out += p.tolist()
    return out


def measure(repo: str, lic: str, rule: str) -> dict:
    """One candidate on both sets (runs inside its own process, see run())."""
    sets = {p.stem: list(read_jsonl(p)) for p in sorted(OUT.glob("*_*.jsonl"))}
    entry = {"license": lic}
    t0 = time.perf_counter()
    tok, model = load_candidate(repo)
    idx = ai_index(model, rule)
    entry["ai_index"] = idx
    entry["params_m"] = round(sum(p.numel() for p in model.parameters()) / 1e6)
    for ds in ("coat", "llmtrace"):
        calib, test = sets.get(f"{ds}_calib", []), sets.get(f"{ds}_test", [])
        if not calib or not test:
            continue
        sc = score(tok, model, idx, [r["text"] for r in calib])
        st = score(tok, model, idx, [r["text"] for r in test])
        yc, yt = [r["label"] for r in calib], [r["label"] for r in test]
        t = choose_threshold(yc, sc, 0.05)
        entry[ds] = {
            "auroc": round(auroc(yt, st), 4),
            "at_0.5": {k: (round(v, 4) if isinstance(v, float) else v)
                       for k, v in rates(yt, st, 0.5).items()},
            "calibrated_threshold": round(t, 4),
            "at_calibrated": {k: (round(v, 4) if isinstance(v, float) else v)
                              for k, v in rates(yt, st, t).items()},
        }
    entry["seconds"] = round(time.perf_counter() - t0, 1)
    return entry


def run() -> dict:
    """Each candidate in a fresh process: memory is fully returned between
    models, and a candidate that cannot fit in RAM is recorded, not fatal."""
    import subprocess
    import sys

    results = OUT / "results"
    results.mkdir(parents=True, exist_ok=True)
    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "candidates": {}}
    for repo, lic, rule in CANDIDATES:
        out = results / (repo.replace("/", "__") + ".json")
        if not out.exists():
            proc = subprocess.run([sys.executable, "-m", "detector.training.ru_eval", "one", repo],
                                  capture_output=True, text=True)
            if proc.returncode != 0 and not out.exists():
                tail = (proc.stderr or "")[-400:]
                reason = ("out of memory (laptop RAM)" if "memory" in tail.lower() or proc.returncode in (3221225477, -1073741819, 3221226505)
                          else f"exit {proc.returncode}")
                out.write_text(json.dumps({"license": lic, "error": reason, "stderr_tail": tail}), encoding="utf-8")
        entry = json.loads(out.read_text(encoding="utf-8"))
        report["candidates"][repo] = entry
        (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(repo, json.dumps({k: v for k, v in entry.items() if k in ("coat", "llmtrace", "error")})[:400],
              flush=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["data", "run", "one"])
    ap.add_argument("repo", nargs="?")
    args = ap.parse_args()
    if args.cmd == "data":
        print(json.dumps(build_sets(), indent=2))
    elif args.cmd == "one":
        lic, rule = next((lic, rule) for repo, lic, rule in CANDIDATES if repo == args.repo)
        entry = measure(args.repo, lic, rule)
        (OUT / "results" / (args.repo.replace("/", "__") + ".json")).write_text(
            json.dumps(entry, indent=2), encoding="utf-8")
    else:
        run()


if __name__ == "__main__":
    main()
