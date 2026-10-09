"""Script 3: train / val / test split.

    python -m detector.training.split [--test 0.10 --val 0.10 --seed 13]

Rules:
* Grouped by SOURCE DOCUMENT: every chunk of one human document and every AI
  twin of those chunks land in the same split. A model must never see one
  half of a pair in training and be tested on the other.
* Stratified by (lang, genre): each stratum gives ~10% of its groups to test
  and ~10% to val, so a rare genre is not missing from the test set.
* SEALED: work/split/assignments.json records every group's split. A later
  run (more data arrived) keeps every known group where it was and only
  places the new ones — a test group never reaches training and a trained-on
  group never becomes test. Delete the file only to start over deliberately.

Output: work/split/{train,val,test}.jsonl with {id, text, label, lang, genre,
group, source}, label 1 = AI, 0 = human; and split_report.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from detector.training.common import read_jsonl, work_dir, write_jsonl


def load_rows(work: Path) -> list[dict]:
    rows = []
    for r in read_jsonl(work / "human_chunks.jsonl"):
        rows.append({"id": r["id"], "text": r["text"], "label": 0, "lang": r["lang"],
                     "genre": r.get("genre") or "unknown", "group": r["doc_id"],
                     "source": "human", "status": r.get("status")})
    for r in read_jsonl(work / "ai_generated.jsonl"):
        if r.get("length_ok") is False and r.get("words", 0) < 60:
            continue  # a truncated answer teaches nothing
        rows.append({"id": r["id"], "text": r["text"], "label": 1, "lang": r["lang"],
                     "genre": r.get("genre") or "unknown", "group": r["doc_id"],
                     "source": f"gen:{r.get('style')}"})
    for r in read_jsonl(work / "ai_manual.jsonl"):
        rows.append({"id": r["id"], "text": r["text"], "label": 1, "lang": r["lang"],
                     "genre": r.get("genre") or "unknown", "group": r["doc_id"],
                     "source": f"manual:{r.get('model')}"})
    return rows


def test_eligible(rows: list[dict]) -> set[str]:
    """Groups allowed into TEST: no human text in them is "tasdiqlanmagan".
    A Telegram essay posted after 2022-11-01 may be human, but nothing
    proves it — measuring false positives on it would measure nothing.
    Unlabelled texts (the teachers' own) are eligible."""
    blocked = {r["group"] for r in rows if r.get("status") == "tasdiqlanmagan"}
    return {r["group"] for r in rows} - blocked


def assign(rows: list[dict], test_frac: float, val_frac: float, seed: int,
           previous: dict[str, str]) -> dict[str, str]:
    """group -> "train" | "val" | "test".

    `previous` is the assignment of every earlier run: those groups NEVER move
    (a group trained on must not later become test data, and a test group
    must not leak into training). Only new groups are placed, each stratum
    topped up towards its test/val share."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        votes[r["group"]][(r["lang"], r["genre"])] += 1
    strata: dict[tuple, list[str]] = defaultdict(list)
    for g, c in votes.items():
        strata[c.most_common(1)[0][0]].append(g)
    # A stratum of 1-2 documents cannot give up a test AND a val group: fold
    # small genres into their language, then small languages into one pool.
    for coarse in (lambda k: (k[0], "*"), lambda k: ("*", "*")):
        merged: dict[tuple, list[str]] = defaultdict(list)
        for key, groups in strata.items():
            merged[coarse(key) if len(groups) < 3 else key].extend(groups)
        strata = merged

    eligible = test_eligible(rows)
    rng = random.Random(seed)
    out: dict[str, str] = {}
    for key in sorted(strata):
        groups = sorted(strata[key])
        n = len(groups)
        want = {
            "test": max(1, round(n * test_frac)) if n >= 3 else 0,
            "val": max(1, round(n * val_frac)) if n >= 3 else 0,
        }
        have = Counter()
        for g in groups:
            if g in previous:
                out[g] = previous[g]
                have[previous[g]] += 1
        new = [g for g in groups if g not in previous]
        rng.shuffle(new)
        for g in new:
            if have["test"] < want["test"] and g in eligible:
                out[g] = "test"
            elif have["val"] < want["val"]:
                out[g] = "val"
            else:
                out[g] = "train"
            have[out[g]] += 1
    return out


def run(data_dir: Path | None, test_frac: float, val_frac: float, seed: int) -> dict:
    work = work_dir(data_dir)
    split_dir = work / "split"
    split_dir.mkdir(parents=True, exist_ok=True)
    assign_file = split_dir / "assignments.json"
    previous = json.loads(assign_file.read_text(encoding="utf-8")) if assign_file.exists() else {}

    rows = load_rows(work)
    where = assign(rows, test_frac, val_frac, seed, previous)
    parts: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    for r in rows:
        parts[where[r["group"]]].append(r)
    for name, part in parts.items():
        write_jsonl(split_dir / f"{name}.jsonl", part)

    # Groups that vanished from the data keep their entry: if they come back
    # they return to the same split.
    merged = {**previous, **where}
    assign_file.write_text(json.dumps(merged, indent=0, sort_keys=True), encoding="utf-8")
    test_groups = sorted(g for g, sp in merged.items() if sp == "test")

    counts = Counter((name, r["lang"], r["genre"], r["label"]) for name, part in parts.items() for r in part)
    with (split_dir / "split_report.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["split", "lang", "genre", "label", "count"])
        for (name, lang, genre, label), n in sorted(counts.items()):
            w.writerow([name, lang, genre, "ai" if label else "human", n])
    return {name: len(part) for name, part in parts.items()} | {
        "sealed_groups": len(test_groups),
        "test_eligible_groups": len(test_eligible(rows)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--test", type=float, default=0.10)
    ap.add_argument("--val", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()
    print(run(args.data_dir, args.test, args.val, args.seed))


if __name__ == "__main__":
    main()
