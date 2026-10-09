"""Measure the detector against its budget: RSS < 1.5 GB, 1000 words < 10 s.

    python -m detector.scripts.benchmark --models detector/models --threads 4
    python -m detector.scripts.benchmark --text-file essay.txt

Without --text-file it scores a built-in ~1000-word English text. Prints
model load time, RSS after load, and time / peak RSS of a full /detect
analysis (document chunks + every sentence), each run several times.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import psutil

PARAGRAPH = (
    "Education has always been one of the most important foundations of any "
    "society. When children learn to read and write, they gain access to the "
    "knowledge of previous generations, and this knowledge allows them to build "
    "something new. Teachers play a central role in this process, because they "
    "do not only explain facts but also show how to think about them. In many "
    "schools the number of students in one class is still too large, and this "
    "makes it difficult for a teacher to pay attention to every child. Parents "
    "can help by reading with their children at home and by talking with them "
    "about what they learned during the day. Technology may also help, although "
    "it cannot replace a good teacher. "
)


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1e6


class PeakSampler:
    def __init__(self) -> None:
        self.peak = rss_mb()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, rss_mb())
            time.sleep(0.02)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="detector/models")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--text-file")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    from detector.service.analysis import analyze
    from detector.service.engine import Registry
    from detector.service.text import word_count

    text = (
        Path(args.text_file).read_text(encoding="utf-8") if args.text_file
        else (PARAGRAPH * 9)
    )
    base = rss_mb()
    t0 = time.perf_counter()
    reg = Registry.from_dir(Path(args.models), threads=args.threads)
    load_s = time.perf_counter() - t0
    after_load = rss_mb()
    if not reg.models:
        raise SystemExit(f"no models under {args.models}")

    runs = []
    with PeakSampler() as peak:
        for _ in range(args.runs):
            t = time.perf_counter()
            r = analyze(text, reg)
            runs.append(time.perf_counter() - t)
    print(json.dumps({
        "models": [m.version for m in reg.models],
        "threads": args.threads,
        "words": word_count(text),
        "sentences": len(r["sentences"]),
        "chunks": r["chunks"],
        "verdict": r["verdict"],
        "ai_probability": r["ai_probability"],
        "load_seconds": round(load_s, 2),
        "rss_mb_before_load": round(base),
        "rss_mb_after_load": round(after_load),
        "rss_mb_peak": round(peak.peak),
        "analyze_seconds": [round(x, 2) for x in runs],
    }, indent=2))


if __name__ == "__main__":
    main()
