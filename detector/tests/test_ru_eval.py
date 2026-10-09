"""Russian candidate evaluation helpers (training/ru_eval.py), no network."""
from __future__ import annotations

import json
from types import SimpleNamespace

from detector.training import ru_eval


def test_iter_lines_splits_only_on_newline_bytes():
    rows = [{"text": "a\rb c"}, {"text": "второй"}]
    raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8")
    # split the stream at awkward places, including inside a UTF-8 character
    chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]
    got = [json.loads(line) for line in ru_eval.iter_lines(chunks)]
    assert got == rows


def test_iter_lines_trailing_line_without_newline():
    assert list(ru_eval.iter_lines([b"x\ny"])) == [b"x", b"y"]


def test_ai_index_reads_label_names_or_defaults_to_1():
    m = SimpleNamespace(config=SimpleNamespace(id2label={0: "ai", 1: "human"}))
    assert ru_eval.ai_index(m, "ai_label") == 0
    m = SimpleNamespace(config=SimpleNamespace(id2label={0: "LABEL_0", 1: "LABEL_1"}))
    assert ru_eval.ai_index(m, "index1") == 1


def test_only_licensed_candidates():
    assert all(lic in ("MIT", "apache-2.0") for _repo, lic, _rule in ru_eval.CANDIDATES)
