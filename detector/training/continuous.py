"""Offline candidate learning from independently reviewed opt-in data, with strict promotion gates."""
from __future__ import annotations
import argparse
import collections
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from detector.service.corpus import CorpusStore
from detector.service.releases import file_hash, set_active, validate_bundle, ReleaseRegistry
from detector.service.lang import detect_language
from detector.service.text import word_count

D = Path(__file__).resolve().parents[1]
WORK = D / "data/work/model_updates"
RELEASES = D / "data/model_updates/releases"
DB = D / "data/feedback/corpus.sqlite3"
SPLIT = D / "data/work/collection_input/work/split"
FOLDERS = {"uz": "uz-latin-linear", "uz-cyrl": "uz-cyrillic-linear", "ru": "ru-ensemble", "en": "en-tmr"}

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)

def read_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf8") if line.strip()]

def snapshot_digest(samples):
    data = [(r["id"], r["content_hash"], r["verified_label"], r["verified_model"], r["reviewed_at"], r["lang"], r["genre"], r["topic"], r["evidence_kind"], hashlib.sha256((r["evidence"] or "").encode()).hexdigest()) for r in sorted(samples, key=lambda r: r["id"])]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()

def group_for(row):
    # Same explicitly provided task/topic stays in one split. Missing topics use document identity.
    topic = re.sub(r"\s+", " ", row["topic"].strip().lower())
    value = row["lang"] + ":" + ("topic:" + topic if topic else "doc:" + row["content_hash"])
    return "fb-" + hashlib.sha256(value.encode()).hexdigest()[:24]

def validation_group(group):
    return not holdout_group(group) and int(hashlib.sha256(("feedback-val13:"+group).encode()).hexdigest()[:8],16)%5==0

def holdout_group(group):
    return int(hashlib.sha256(("feedback-fixed13:" + group).encode()).hexdigest()[:8], 16) % 5 == 0

def shingles(text):
    tokens = re.findall(r"[^\W\d_]+(?:['ʻʼ‘’\x60][^\W\d_]+)*", text.lower())
    return {hashlib.blake2b(" ".join(tokens[i:i+5]).encode(), digest_size=8).digest() for i in range(max(0, len(tokens)-4))}

def record_signature(row):
    if "shingle_hashes" in row:
        return {bytes.fromhex(value) for value in row["shingle_hashes"]}
    return shingles(row["text"])

def private_ledger_row(row):
    # Do not duplicate consented text/topic in work directories. Withdrawal
    # removes the sole raw corpus record; fingerprints remain for split hygiene.
    result = {key:row[key] for key in ("id","doc_id","group","lang","label","words")}
    result["shingle_hashes"] = sorted(value.hex() for value in shingles(row["text"]))
    return result

def near(a, b):
    common = len(a & b)
    return common / max(1, len(a | b)) >= .8 or common / max(1, min(len(a), len(b))) >= .95

def prepare(samples, protected):
    from detector.training.prepare_human import chunk_paragraphs
    # Compare paragraph chunks to frozen VAL/TEST and to every accepted feedback chunk.
    protected_shingles = [record_signature(r) for r in protected]
    seen, accepted, exclusions = [], [], []
    for row in samples:
        if row["lang"] not in FOLDERS or detect_language(row["text"]) != row["lang"]:
            exclusions.append({"id": row["id"], "reason": "unsupported_or_mismatched_language"}); continue
        chunks = []
        for paragraph in chunk_paragraphs(row["text"], 150, 350):
            matches = list(re.finditer(r"[^\W\d_]+(?:['ʻʼ‘’\x60][^\W\d_]+)*", paragraph))
            count = len(matches)
            parts = max(1, (count + 349) // 350)
            boundaries = [round(i * count / parts) for i in range(parts + 1)]
            for a, b in zip(boundaries, boundaries[1:]):
                if b - a < 150: continue
                start = 0 if a == 0 else matches[a-1].end()
                end = len(paragraph) if b == count else matches[b-1].end()
                chunks.append(paragraph[start:end].strip())
        signatures = [shingles(text) for text in chunks]
        if not chunks or any(near(a, b) for a in signatures for b in protected_shingles + seen):
            exclusions.append({"id": row["id"], "reason": "short_or_near_duplicate_of_holdout_or_feedback"}); continue
        group = group_for(row)
        for i, text in enumerate(chunks[:3]):
            accepted.append({"id": f'fb-{row["id"]}-{i}', "doc_id": "fb-doc-" + row["id"],
                "group": group, "text": text, "label": row["verified_label"], "lang": row["lang"],
                "genre": row["genre"], "topic": row["topic"], "words": word_count(text),
                "source": "independently_reviewed_opt_in", "source_file": row["id"], "status": "approved",
                "model": row["verified_model"], "evidence_kind": row["evidence_kind"]})
        seen.extend(signatures)
    return accepted, exclusions

def group_metrics(rows, scores, threshold, training):
    # Aggregate chunks by document, then use worst case per independent task group.
    docs = training.grouped([dict(r, group=r["doc_id"]) for r in rows], scores)
    group_map = {r["doc_id"]: r["group"] for r in rows}
    buckets = collections.defaultdict(list)
    for doc in docs: buckets[(group_map[doc["group"]], doc["label"])].append(doc["score"])
    conservative = [{"label": label, "score": max(values) if label == 0 else min(values)}
                    for (group, label), values in buckets.items()]
    return training.summarize(conservative, threshold)

def acceptance_gate(candidate, baseline):
    # These rules are predeclared; insufficient evidence never auto-promotes a model.
    checks = {
        "minimum_100_human_groups": candidate["human"] >= 100,
        "minimum_50_ai_groups": candidate["ai"] >= 50,
        "upper_95_percent_fpr_at_most_5_percent": candidate["fpr_95_ci"][1] is not None and candidate["fpr_95_ci"][1] <= .05,
        "ai_recall_at_least_80_percent": candidate["tpr"] is not None and candidate["tpr"] >= .8,
        "not_worse_human_errors": candidate["fp"] <= baseline["fp"],
        "not_worse_ai_recall": candidate["tp"] >= baseline["tp"],
    }
    return all(checks.values()), checks

def _attempt(store, auto_promote=False):
    samples = store.approved()
    digest = snapshot_digest(samples)
    state_path = WORK / "worker_state.json"
    previous = json.loads(state_path.read_text(encoding="utf8")) if state_path.exists() else {}
    if previous.get("snapshot_digest") == digest and previous.get("state") not in {"waiting_for_RAM", "private_training_dataset_not_installed"}:
        return {"state": "unchanged", "approved": len(samples)}
    summary = {"snapshot_digest": digest, "approved": len(samples), "state": "waiting_for_verified_pairs", "active_model_changed": False, "english_learning": "English candidate uses fixed grouped opt-in TRAIN/VAL/TEST; incumbent preserved until gates pass"}
    # Avoid loading training dependencies or model weights until there is adequate reviewed data.
    counts = collections.Counter((r["lang"], r["verified_label"]) for r in samples)
    eligible = [lang for lang in FOLDERS if min(counts[lang,0], counts[lang,1]) >= 15]
    if not eligible:
        save(state_path, summary); return summary
    if not all((SPLIT/name).is_file() for name in ("train_aug.jsonl", "val.jsonl", "test.jsonl")):
        summary["state"] = "private_training_dataset_not_installed"
        save(state_path, summary); return summary
    import psutil
    if psutil.virtual_memory().available < 1.25 * (1 << 30):
        summary["state"]="waiting_for_RAM"
        save(state_path,summary); return summary
    from detector.training import train_local as tr
    tr.LANGUAGES.setdefault("en","en-feedback-linear")
    from threadpoolctl import threadpool_limits
    base_train = read_rows(SPLIT / "train_aug.jsonl") + read_rows(D / "data/work/essay_supplement/train_aug.jsonl")
    val = read_rows(SPLIT / "val.jsonl")
    protected = base_train + val + read_rows(SPLIT / "test.jsonl")
    used_path = WORK / "evaluated_holdout_groups.json"
    used = set(json.loads(used_path.read_text(encoding="utf8"))) if used_path.exists() else set()
    protected += [r for path in WORK.glob("*/fresh_holdout.jsonl") for r in read_rows(path) if r["group"] in used]
    ordered = sorted(samples, key=lambda r: (group_for(r) not in used, not holdout_group(group_for(r)), r["id"]))
    fresh, exclusions = prepare(ordered, protected)
    fresh_validation = [r for r in fresh if r["lang"]=="en" and validation_group(r["group"])]
    fresh_train = [r for r in fresh if not holdout_group(r["group"]) and not (r["lang"]=="en" and validation_group(r["group"]))]
    val += fresh_validation
    fresh_test = [r for r in fresh if holdout_group(r["group"]) and r["group"] not in used]
    # A later topic change or newly submitted paraphrase must not make an already
    # trained document appear as fresh evidence for the next champion.
    previous_training = [record_signature(r) for path in WORK.glob("*/train.jsonl") for r in read_rows(path)]
    contaminated = {r["doc_id"] for r in fresh_test if any(near(shingles(r["text"]), sig) for sig in previous_training)}
    fresh_test = [r for r in fresh_test if r["doc_id"] not in contaminated]
    exclusions.extend({"id":sid, "reason":"near_duplicate_of_previous_feedback_training"} for sid in sorted(contaminated))
    assert not {r["group"] for r in fresh_train} & {r["group"] for r in fresh_test}
    fresh_counts = {lang: {str(label):len({r["group"] for r in fresh_test if r["lang"]==lang and r["label"]==label}) for label in (0,1)} for lang in eligible}
    ready = [lang for lang in eligible if fresh_counts[lang]["0"]>=100 and fresh_counts[lang]["1"]>=50]
    if not ready:
        summary.update(state="waiting_for_fresh_independent_groups", fresh_holdout_groups=fresh_counts, exclusions=len(exclusions))
        save(state_path,summary); return summary
    eligible = ready
    version = "feedback-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "-" + digest[:8]
    work = WORK / version
    save(work / "snapshot.json", {"digest": digest, "sample_ids": [r["id"] for r in samples], "exclusions": exclusions})
    # Persist only anti-leak fingerprints. Raw student text stays solely in the consented SQLite corpus.
    for name, rows in (("train.jsonl", fresh_train), ("fresh_holdout.jsonl", fresh_test), ("validation.jsonl", fresh_validation)):
        with (work / name).open("w", encoding="utf8") as f:
            for row in rows: f.write(json.dumps(private_ledger_row(row), ensure_ascii=False) + "\n")
    manager = ReleaseRegistry(D / "models", RELEASES, threads=2, languages=set(FOLDERS))
    baseline = manager.get()
    reports, trained, passed = {}, {}, []
    for lang in eligible:
        train_docs = {(r["doc_id"],r["label"]) for r in fresh_train if r["lang"] == lang}
        if any(sum(label == target for _,label in train_docs) < 10 for target in (0,1)):
            continue
        if len({r["group"] for r in fresh_train if r["lang"]==lang}) < 3:
            continue
        if any(sum(1 for r in val if r["lang"]==lang and r["label"]==label)<3 for label in (0,1)):
            reports[lang]={"gate_passed":False,"reason":"independent fixed validation needs both labels and 3+ chunks each"}
            continue
        with threadpool_limits(limits=2):
            model, report = tr.train_one(lang, base_train + fresh_train, val, work / "candidates")
        meta_path = model.dir / "detector_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf8"))
        meta.update(version=version+"-"+lang, source="Baseline TRAIN plus independently approved opt-in corpus; untouched fixed VAL and new grouped feedback holdout",
                    feedback_snapshot_sha256=digest, school_essay_accuracy_proven=False)
        save(meta_path, meta)
        # Candidate and threshold are frozen BEFORE fresh holdout scoring.
        report["frozen_meta_sha256"] = file_hash(meta_path)
        rows = [r for r in fresh_test if r["lang"] == lang]
        if not rows or {r["label"] for r in rows} != {0,1}:
            report.update(gate_passed=False, reason="fresh holdout needs both labels")
        else:
            # Reserve the cohort before evaluation, even if scoring fails. Never use it as fresh evidence twice.
            used.update(r["group"] for r in rows)
            save(used_path, sorted(used))
            candidate_metrics = group_metrics(rows, [model.score_document(r["text"])[0] for r in rows], model.threshold, tr)
            champion = baseline.for_language(lang)
            baseline_metrics = group_metrics(rows, [champion.score_document(r["text"])[0] for r in rows], champion.threshold, tr)
            ok, checks = acceptance_gate(candidate_metrics, baseline_metrics)
            # Russian external regressions additionally prevent loss of the existing ensemble's breadth.
            if lang == "ru":
                checks["external_russian_no_regression"] = True
                for family in ("coat","llmtrace"):
                    external = [dict(r,group=r["id"],doc_id=r["id"],lang="ru",words=word_count(r["text"])) for r in read_rows(D/"data/ru_eval"/(family+"_test.jsonl")) if word_count(r["text"])>=150 and detect_language(r["text"])=="ru"]
                    cm = tr.summarize([dict(r,score=model.score_document(r["text"])[0]) for r in external], model.threshold)
                    bm = tr.summarize([dict(r,score=champion.score_document(r["text"])[0]) for r in external], champion.threshold)
                    report[family+"_reused_regression"] = {"candidate":cm,"baseline":bm}
                    checks["external_russian_no_regression"] &= cm["fp"] <= bm["fp"] and cm["tp"] >= bm["tp"]
            report.update(gate_passed=all(checks.values()), acceptance_checks=checks,
                          fresh_group_holdout=candidate_metrics, baseline_fresh_group_holdout=baseline_metrics)
        assert file_hash(meta_path) == report["frozen_meta_sha256"]
        reports[lang], trained[lang] = report, model.dir
        if report["gate_passed"]: passed.append(lang)
    save(work / "report.json", {"version":version,"snapshot_digest":digest,"languages":reports,"promotable_languages":passed})
    summary.update(state="candidate_ready" if passed else "candidate_not_promoted", candidate=version)
    if passed:
        folder = RELEASES / version
        assert not folder.exists()
        models = folder / "models"
        for lang, name in FOLDERS.items():
            source = trained[lang] if lang in passed else baseline.for_language(lang).dir
            shutil.copytree(source, models / name)
        artifacts = [{"file":p.relative_to(folder).as_posix(),"sha256":file_hash(p)} for p in models.rglob("*") if p.is_file()]
        save(folder / "manifest.json", {"gate_passed":True,"snapshot_digest":digest,"version":version,
                    "changed_languages":passed,"artifacts":artifacts,"metrics_report":str(work/"report.json")})
        if auto_promote:
            if snapshot_digest(store.approved()) != digest:
                summary["state"] = "consent_or_review_snapshot_changed"
            else:
                set_active(RELEASES, version); summary.update(state="promoted", active_model_changed=True)
    save(state_path, summary)
    return summary

def attempt(store, auto_promote=False):
    # Only one offline learner may consume a fresh cohort or publish at a time.
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK/"worker.lock").open("a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: return {"state":"another_worker_is_running"}
        else:
            import fcntl
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: return {"state":"another_worker_is_running"}
        try: return _attempt(store, auto_promote)
        finally:
            lock.seek(0)
            if os.name == "nt": msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["once","watch","promote","rollback","baseline"])
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--release")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--auto-promote", action="store_true")
    args = p.parse_args()
    if args.interval < 30: p.error("interval must be at least 30 seconds")
    store = CorpusStore(args.db)
    if args.action in {"promote","rollback"}:
        if not args.release: p.error("--release required")
        if args.action == "promote":
            manifest = validate_bundle(RELEASES/args.release)
            if manifest["snapshot_digest"] != snapshot_digest(store.approved()):
                raise ValueError("review/consent changed since candidate training")
        set_active(RELEASES,args.release)
        print(json.dumps({"active_release":args.release})); return
    if args.action == "baseline":
        pointer = RELEASES / "active.json"
        if pointer.exists():
            os.replace(pointer, RELEASES/("inactive-"+str(time.time_ns())+".json"))
        print(json.dumps({"active_release":"baseline"})); return
    while True:
        try: print(json.dumps(attempt(store,args.auto_promote)),flush=True)
        except Exception as exc:
            save(WORK/"last_worker_error.json",{"error_type":type(exc).__name__,"active_release_unchanged_by_failed_attempt":True})
            if args.action=="once": raise
        if args.action=="once": break
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
