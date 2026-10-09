"""Step 6: metrics, the decision threshold, and the CSV for the QuillBot comparison.

    # score with the exported ONNX model (the artefact that will be deployed)
    python -m detector.training.evaluate --model-dir detector/models/uzru-xlmr
    # or with scores computed elsewhere (CSV with columns id, score in 0..1)
    python -m detector.training.evaluate --val-scores val.csv --test-scores test.csv

The threshold is chosen on VAL — the lowest one at which at most --max-fpr
(5%) of HUMAN texts would be called AI — and only then measured on TEST, so
the test numbers are honest. With --write-threshold it is stored in the
model's detector_meta.json, which the service reads.

Outputs in work/eval/:
    report.json         accuracy, AUROC, FPR, TPR on val/test(/test_ocr), per language and genre
    test_scores.csv     text_id, haqiqiy, bizning_ball, quillbot_ball
                        (quillbot_ball is left empty for manual filling;
                         bizning_ball is 0-100 like QuillBot's percentage)
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from detector.training.common import read_jsonl, work_dir


def auroc(labels: list[int], scores: list[float]) -> float | None:
    """Mann-Whitney U with average ranks for ties. None if one class is missing."""
    pos = sum(labels)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_pos = sum(r for r, y in zip(ranks, labels) if y == 1)
    return (rank_pos - pos * (pos + 1) / 2) / (pos * neg)


def rates(labels: list[int], scores: list[float], t: float) -> dict:
    tp = sum(1 for y, s in zip(labels, scores) if y == 1 and s >= t)
    fp = sum(1 for y, s in zip(labels, scores) if y == 0 and s >= t)
    pos = sum(labels)
    neg = len(labels) - pos
    correct = sum(1 for y, s in zip(labels, scores) if (s >= t) == (y == 1))
    return {
        "n": len(labels), "human": neg, "ai": pos,
        "accuracy": correct / len(labels) if labels else None,
        "fpr": fp / neg if neg else None,       # human called AI
        "tpr": tp / pos if pos else None,       # AI caught
    }


def choose_threshold(labels: list[int], scores: list[float], max_fpr: float) -> float:
    """Lowest threshold with FPR <= max_fpr on these (validation) scores."""
    human = sorted((s for y, s in zip(labels, scores) if y == 0), reverse=True)
    if not human:
        raise ValueError("no human texts to calibrate on")
    allowed = int(max_fpr * len(human))      # this many humans may score >= t
    if allowed >= len(human):
        return 0.0
    # Just above the (allowed+1)-th highest human score.
    return min(1.0, human[allowed] + 1e-6)


def summarize(rows: list[dict], scores: dict[str, float], t: float) -> dict:
    rows = [r for r in rows if r["id"] in scores]
    labels = [r["label"] for r in rows]
    sc = [scores[r["id"]] for r in rows]
    out = rates(labels, sc, t) | {"auroc": auroc(labels, sc)}
    for key in ("lang", "genre"):
        groups = defaultdict(list)
        for r in rows:
            groups[r[key]].append(r)
        out[f"by_{key}"] = {
            g: rates([r["label"] for r in rs], [scores[r["id"]] for r in rs], t)
            | {"auroc": auroc([r["label"] for r in rs], [scores[r["id"]] for r in rs])}
            for g, rs in sorted(groups.items())
        }
    return out


def score_with_model(model_dir: Path, rows: list[dict]) -> dict[str, float]:
    from detector.service.engine import OnnxDetector

    det = OnnxDetector(model_dir)
    return {r["id"]: det.score_document(r["text"])[0] for r in rows}


def read_scores(path: Path) -> dict[str, float]:
    with path.open(encoding="utf-8-sig") as f:
        return {row["id"]: float(row["score"]) for row in csv.DictReader(f)}


def write_comparison_csv(path: Path, rows: list[dict], scores: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # BOM: Excel opens UTF-8 right
        w = csv.writer(f)
        w.writerow(["text_id", "haqiqiy", "bizning_ball", "quillbot_ball"])
        for r in rows:
            if r["id"] in scores:
                w.writerow([r["id"], "si" if r["label"] else "odam",
                            f"{scores[r['id']] * 100:.1f}", ""])


def run(data_dir: Path | None, *, model_dir: Path | None, val_scores: Path | None,
        test_scores: Path | None, max_fpr: float, write_threshold: bool) -> dict:
    work = work_dir(data_dir)
    split = work / "split"
    val = list(read_jsonl(split / "val.jsonl"))
    test = list(read_jsonl(split / "test.jsonl"))
    test_ocr = list(read_jsonl(split / "test_ocr.jsonl"))

    if model_dir:
        sv = score_with_model(model_dir, val)
        st = score_with_model(model_dir, test + test_ocr)
    else:
        if not (val_scores and test_scores):
            raise SystemExit("give --model-dir, or both --val-scores and --test-scores")
        sv, st = read_scores(val_scores), read_scores(test_scores)

    t = choose_threshold([r["label"] for r in val if r["id"] in sv],
                         [sv[r["id"]] for r in val if r["id"] in sv], max_fpr)
    report = {
        "threshold": t,
        "max_fpr_target": max_fpr,
        "val": summarize(val, sv, t),
        "test": summarize(test, st, t),
    }
    if test_ocr:
        report["test_ocr"] = summarize(test_ocr, st, t)

    out = work / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_comparison_csv(out / "test_scores.csv", test, st)

    if write_threshold and model_dir:
        meta_path = model_dir / "detector_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["threshold"] = round(t, 4)
        meta["threshold_note"] = f"val FPR <= {max_fpr:.0%}; test FPR {report['test']['fpr']}"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--model-dir", type=Path)
    ap.add_argument("--val-scores", type=Path)
    ap.add_argument("--test-scores", type=Path)
    ap.add_argument("--max-fpr", type=float, default=0.05)
    ap.add_argument("--write-threshold", action="store_true")
    args = ap.parse_args()
    r = run(args.data_dir, model_dir=args.model_dir, val_scores=args.val_scores,
            test_scores=args.test_scores, max_fpr=args.max_fpr,
            write_threshold=args.write_threshold)
    brief = {k: {m: r[k][m] for m in ("n", "accuracy", "auroc", "fpr", "tpr")}
             for k in ("val", "test", "test_ocr") if k in r and r[k]["n"]}
    if not r["test"]["n"]:
        print("WARNING: the test set is EMPTY (no clean, pre-2022-11 human texts?). "
              "Only validation numbers below; they chose the threshold, so they are optimistic.")
    print(json.dumps({"threshold": r["threshold"], **brief}, indent=2))


if __name__ == "__main__":
    main()
