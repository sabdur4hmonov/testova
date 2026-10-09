"""Train and freeze three small offline classifiers; TEST is never used for fitting."""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
DETECTOR = ROOT / "detector"
sys.path.insert(0, str(ROOT))
local_libs = DETECTOR / "data/work/training_runtime_libs"
if local_libs.is_dir():
    sys.path.insert(0, str(local_libs))
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

import numpy as np
import psutil
from scipy.stats import beta
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold
from threadpoolctl import threadpool_limits

from detector.service.linear import BACKEND, FEATURE_VERSION, LinearDetector, feature_tokens, sigmoid

LANGUAGES = {"uz": "uz-latin-linear", "uz-cyrl": "uz-cyrillic-linear", "ru": "ru-linear"}
VERSION = "testova-linear-v1.1-20261007"


def read_rows(path):
    with path.open(encoding="utf8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")


def grouped(rows, values):
    buckets = collections.defaultdict(list)
    for row, value in zip(rows, values):
        buckets[(row["group"], row["label"], row["lang"])].append((row, float(value)))
    result = []
    for (group, label, lang), items in sorted(buckets.items()):
        weight = sum(r["words"] for r, _ in items)
        score = sum(v * r["words"] for r, v in items) / weight
        result.append({"group": group, "label": label, "lang": lang,
                       "genre": items[0][0]["genre"], "score": score,
                       "chunks": len(items)})
    return result


def sample_weights(rows):
    """Equal source documents within each genre/class; augmentations remain grouped."""
    count = collections.Counter((r["group"], r["label"], r["genre"]) for r in rows)
    weights = np.array([1 / count[(r["group"], r["label"], r["genre"])] for r in rows])
    strata = collections.defaultdict(list)
    for i, r in enumerate(rows):
        strata[(r["genre"], r["label"])].append(i)
    for indices in strata.values():
        weights[indices] *= len(indices) / weights[indices].sum()
    return weights


def new_vectorizer():
    return TfidfVectorizer(analyzer=feature_tokens, lowercase=False, sublinear_tf=True,
                           min_df=2, max_features=30_000, dtype=np.float64)


def classifier(c):
    return LogisticRegression(C=c, solver="liblinear", max_iter=500, tol=1e-5,
                              random_state=13)


def aggregate_auc(rows, margins):
    docs = grouped(rows, margins)
    return float(roc_auc_score([r["label"] for r in docs], [r["score"] for r in docs]))


def choose_threshold(human_scores, max_fpr=0.01):
    if not human_scores:
        raise ValueError("validation lacks human documents")
    allowed = int(len(human_scores) * max_fpr)
    ordered = sorted(human_scores, reverse=True)
    # Strictly above the next disallowed human. Ties cannot exceed the budget.
    return max(0.5, min(1.0, float(np.nextafter(ordered[min(allowed, len(ordered) - 1)], 1.0))))


def summarize(records, threshold):
    labels = np.array([r["label"] for r in records])
    scores = np.array([r["score"] for r in records])
    predictions = scores >= threshold
    n0, n1 = int((labels == 0).sum()), int((labels == 1).sum())
    fp = int(((labels == 0) & predictions).sum())
    tp = int(((labels == 1) & predictions).sum())
    tn, fn = n0 - fp, n1 - tp
    def ci(k, n):
        if not n:
            return [None, None]
        return [0.0 if not k else float(beta.ppf(.025, k, n - k + 1)),
                1.0 if k == n else float(beta.ppf(.975, k + 1, n - k))]
    return {"human": n0, "ai": n1, "fp": fp, "tp": tp, "tn": tn, "fn": fn,
            "fpr": fp / n0 if n0 else None, "tpr": tp / n1 if n1 else None,
            "fpr_95_ci": ci(fp, n0), "tpr_95_ci": ci(tp, n1),
            "accuracy": (tn + tp) / max(1, len(records)),
            "balanced_accuracy": ((tn / n0) + (tp / n1)) / 2 if n0 and n1 else None,
            "auroc": float(roc_auc_score(labels, scores)) if n0 and n1 else None}


def train_one(lang, train, val, candidates):
    rows = [r for r in train if r["lang"] == lang]
    labels = np.array([r["label"] for r in rows])
    groups = np.array([r["group"] for r in rows])
    folds = list(StratifiedGroupKFold(n_splits=3, shuffle=True, random_state=13)
                 .split(np.zeros(len(rows)), labels, groups))
    oof = {c: np.zeros(len(rows)) for c in (.5, 2.0, 8.0)}
    for fold, (learn, held) in enumerate(folds):
        assert not set(groups[learn]) & set(groups[held])
        learning = [rows[i] for i in learn]
        vector = new_vectorizer()
        x = vector.fit_transform(r["text"] for r in learning)
        xh = vector.transform(rows[i]["text"] for i in held)
        weights = sample_weights(learning)
        for c in oof:
            fitted = classifier(c).fit(x, labels[learn], sample_weight=weights)
            oof[c][held] = fitted.decision_function(xh)
        print(json.dumps({"language": lang, "cv_fold_done": fold + 1,
                          "rss_mib": round(psutil.Process().memory_info().rss / 2**20, 1)}), flush=True)
    aucs = {c: aggregate_auc(rows, margins) for c, margins in oof.items()}
    best = max(aucs, key=lambda c: (aucs[c], -c))
    documents = grouped(rows, oof[best])
    calibrator = LogisticRegression(C=1.0, solver="lbfgs", max_iter=500).fit(
        np.array([r["score"] for r in documents]).reshape(-1, 1),
        [r["label"] for r in documents])
    a, b = float(calibrator.coef_[0, 0]), float(calibrator.intercept_[0])
    if a <= 0:
        raise ValueError("OOF classifier is not predictive")
    vector = new_vectorizer()
    x = vector.fit_transform(r["text"] for r in rows)
    fitted = classifier(best).fit(x, labels, sample_weight=sample_weights(rows))
    vr = [r for r in val if r["lang"] == lang]
    val_margins = fitted.decision_function(vector.transform(r["text"] for r in vr))
    val_probs = [sigmoid(a * value + b) for value in val_margins]
    val_docs = grouped(vr, val_probs)
    threshold = choose_threshold([r["score"] for r in val_docs if r["label"] == 0])
    oof_probs = [sigmoid(a * value + b) for value in oof[best]]
    oof_docs = grouped(rows, oof_probs)
    weak_essay_humans = [r["score"] for r in oof_docs if r["genre"] == "insho" and r["label"] == 0]
    if weak_essay_humans:
        threshold = max(threshold, choose_threshold(weak_essay_humans, max_fpr=.05))
    out = candidates / LANGUAGES[lang]
    out.mkdir(parents=True, exist_ok=True)
    features = [None] * len(vector.vocabulary_)
    for token, index in vector.vocabulary_.items():
        features[index] = token
    save(out / "vectorizer.json", {"feature_version": FEATURE_VERSION, "features": features})
    np.savez_compressed(out / "weights.npz", idf=vector.idf_, weights=fitted.coef_[0],
                        intercept=fitted.intercept_[0], calibration_a=a, calibration_b=b)
    meta = {"name": f"Testova offline {lang}", "version": VERSION, "languages": [lang],
            "backend": BACKEND, "threshold": threshold, "mixed_band": threshold - .35,
            "artifact_sha256": {name: sha(out / name) for name in ("vectorizer.json", "weights.npz")},
            "source": "local paired corpus; sealed assignments; 300 Gemini-generated originals",
            "license": "Locally trained; corpus attributions and license manifest must accompany distribution",
            "trained_date": "2026-10-06", "calibration": "sigmoid on TRAIN grouped OOF margins",
            "threshold_selection": "VAL document FPR <= 1%; positive verdict probability floor 0.5; TEST excluded from fitting",
            "estimated_probability_not_proof": True, "sentence_min_words": 150}
    save(out / "detector_meta.json", meta)
    runtime = LinearDetector(out)
    parity = max(abs(runtime.probability(r["text"]) - p) for r, p in zip(vr, val_probs))
    assert parity < 1e-8, parity
    return runtime, {"train_rows": len(rows), "train_documents": len(documents),
                     "cv_auroc_by_C": aucs, "selected_C": best, "threshold": threshold,
                     "val_documents": summarize(val_docs, threshold),
                     "train_oof_by_genre": {genre: summarize([r for r in oof_docs if r["genre"] == genre], threshold) for genre in sorted({r["genre"] for r in oof_docs})},
                     "oof_metrics_are_not_independent_test": True,
                     "export_probability_max_delta": parity}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split-dir", type=Path,
                        default=DETECTOR / "data/work/collection_input/work/split")
    parser.add_argument("--out-dir", type=Path, default=DETECTOR / "models")
    args = parser.parse_args()
    started = time.perf_counter()
    work = DETECTOR / "data/work/local_training"
    candidates = work / "frozen_models"
    train = read_rows(args.split_dir / "train_aug.jsonl")
    val = read_rows(args.split_dir / "val.jsonl")
    assert not {r["group"] for r in train} & {r["group"] for r in val}
    assert all(r["status"] != "tasdiqlanmagan" for r in val)
    reports, models = {}, {}
    with threadpool_limits(limits=2):
        for lang in LANGUAGES:
            models[lang], reports[lang] = train_one(lang, train, val, candidates)
            save(work / "frozen_validation_results.json", reports)
        # Freeze every model and threshold before reading any TEST content.
        frozen = {lang: sha(model.dir / "detector_meta.json") for lang, model in models.items()}
        save(work / "freeze.json", frozen)
        test = read_rows(args.split_dir / "test.jsonl")
        for left, right in ((train, test), (val, test)):
            assert not {r["group"] for r in left} & {r["group"] for r in right}
        assert all(r["status"] != "tasdiqlanmagan" for r in test)
        scored = []
        for lang, model in models.items():
            rows = [r for r in test if r["lang"] == lang]
            values = [model.score_document(r["text"])[0] for r in rows]
            docs = grouped(rows, values)
            reports[lang]["test_documents"] = summarize(docs, model.threshold)
            reports[lang]["test_chunks"] = summarize(
                [dict(r, score=v) for r, v in zip(rows, values)], model.threshold)
            reports[lang]["test_by_genre"] = {
                genre: summarize([r for r in docs if r["genre"] == genre], model.threshold)
                for genre in sorted({r["genre"] for r in docs})}
            reports[lang]["confidence_target_proven"] = (
                reports[lang]["test_documents"]["fpr_95_ci"][1] <= .05)
            scored.extend(dict(id=r["id"], group=r["group"], label=r["label"],
                               lang=lang, genre=r["genre"], score=v) for r, v in zip(rows, values))
            assert sha(model.dir / "detector_meta.json") == frozen[lang]
            target = args.out_dir / LANGUAGES[lang]
            target.mkdir(parents=True, exist_ok=True)
            for name in ("vectorizer.json", "weights.npz", "detector_meta.json"):
                (target / name).write_bytes((model.dir / name).read_bytes())
            print(json.dumps({"language": lang, "test_documents": reports[lang]["test_documents"]}),
                  flush=True)
    with (work / "test_scores.csv").open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(scored[0]))
        writer.writeheader()
        writer.writerows(scored)
    result = {"date": "2026-10-06", "version": VERSION, "backend": BACKEND,
              "training_data_sha256": {name: sha(args.split_dir / name)
                                      for name in ("train_aug.jsonl", "val.jsonl", "test.jsonl",
                                                   "assignments.json")},
              "languages": reports, "elapsed_seconds": time.perf_counter() - started,
              "rss_mib_at_end": psutil.Process().memory_info().rss / 2**20,
              "inference_requires_api_key": False, "paid_api_calls": 0,
              "canonical_data_modified": False, "test_used_for_fit_or_threshold": False,
              "ocr_drafts_included": False, "school_essay_accuracy_proven": False,
              "generalization_to_non_gemini_generators_proven": False,
              "production_fpr_bound_proven": all(r["confidence_target_proven"] for r in reports.values())}
    save(work / "report.json", result)
    save(args.out_dir / "local_training_report.json", result)
    print(json.dumps({"completed": True, "elapsed_seconds": result["elapsed_seconds"],
                      "production_fpr_bound_proven": result["production_fpr_bound_proven"]}), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf8")
    main()