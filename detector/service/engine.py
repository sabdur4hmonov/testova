"""ONNX inference for AI-text detectors.

Every model lives in its own directory under DETECTOR_MODELS_DIR:

    model.onnx             the (INT8-quantized) classifier
    tokenizer.json         a `tokenizers` fast-tokenizer file
    detector_meta.json     written by scripts/export_onnx.py

`detector_meta.json` says which languages the model serves, how to read its
output, its special-token ids and its decision threshold. Adding the Uzbek/
Russian model from the A2 pipeline is therefore "drop a directory in" — no
code change: its meta lists ["uz", "uz-cyrl", "ru"].

No PyTorch here: onnxruntime + tokenizers keep the service image small and
its RAM well under the 1.5 GB budget.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

logger = logging.getLogger("detector.engine")

META_FILE = "detector_meta.json"


class Scorer(Protocol):
    """What the analysis code needs from a model. The ONNX model below is the
    real one; tests pass a fake."""

    name: str
    version: str
    languages: list[str]
    threshold: float
    mixed_band: float

    def score_document(self, text: str) -> tuple[float, int]:
        """(AI probability for the whole text, number of chunks scored)."""

    def score_sentences(self, sentences: Sequence[str]) -> list[float]:
        """AI probability of each sentence, in order."""


@dataclass
class ModelMeta:
    name: str
    version: str
    languages: list[str]
    output: str = "softmax2"         # "softmax2" (2 logits) | "sigmoid1" (1 logit)
    ai_index: int = 1                # softmax2: which logit is "AI"
    max_length: int = 512
    cls_id: int = 0
    sep_id: int = 2
    pad_id: int = 1
    threshold: float = 0.5
    mixed_band: float = 0.15
    source: str = ""
    license: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ModelMeta":
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        return cls(
            **{k: v for k, v in raw.items() if k in known},
            extra={k: v for k, v in raw.items() if k not in known},
        )


def _softmax_ai(logits: np.ndarray, ai_index: int) -> np.ndarray:
    z = logits - logits.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return (e / e.sum(axis=-1, keepdims=True))[:, ai_index]


def _sigmoid(logits: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-logits.reshape(-1)))


class OnnxDetector:
    """One exported detector model."""

    # Sentences are short; 128 tokens covers all but run-ons, and keeps the
    # sentence pass a fraction of the document pass.
    min_words = 150
    SENTENCE_MAX_TOKENS = 128
    BATCH = 16
    CHUNK_STRIDE = 64   # tokens of overlap between neighbouring 512-token chunks

    def __init__(self, model_dir: Path, threads: int | None = None) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()

        self.dir = Path(model_dir)
        self.meta = ModelMeta.load(self.dir / META_FILE)
        self.name = self.meta.name
        self.version = self.meta.version
        self.languages = list(self.meta.languages)
        self.threshold = float(self.meta.threshold)
        self.mixed_band = float(self.meta.mixed_band)

        import hashlib
        for filename, digest in self.meta.extra.get("artifact_sha256", {}).items():
            if filename not in ({"model.onnx", "tokenizer.json", "inference.json"} if self.meta.extra.get("backend") == "profile-onnx-v1" else {"model.onnx", "tokenizer.json"}):
                raise ValueError("invalid ONNX artifact")
            h = hashlib.sha256()
            with (self.dir / filename).open("rb") as stream:
                for block in iter(lambda:stream.read(1 << 20),b""): h.update(block)
            if h.hexdigest() != digest: raise ValueError("ONNX artifact checksum mismatch")
        self.tokenizer = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
        self.tokenizer.no_padding()
        self.tokenizer.no_truncation()

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        # Memory pattern/arena on CPU grow with the largest batch seen and are
        # never returned; with variable-length batches that ratchets RSS up.
        so.enable_cpu_mem_arena = False
        self.session = ort.InferenceSession(
            str(self.dir / "model.onnx"), sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {i.name for i in self.session.get_inputs()}

    # ── tokenization ────────────────────────────────────────────────────────
    def _ids(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def _chunks(self, ids: list[int], body: int) -> list[list[int]]:
        if len(ids) <= body:
            return [ids]
        step = max(1, body - self.CHUNK_STRIDE)
        out = []
        for start in range(0, len(ids), step):
            out.append(ids[start:start + body])
            if start + body >= len(ids):
                break
        return out

    def _run(self, seqs: list[list[int]]) -> np.ndarray:
        """AI probability per token sequence (special tokens added here)."""
        probs: list[np.ndarray] = []
        m = self.meta
        for i in range(0, len(seqs), self.BATCH):
            batch = [[m.cls_id, *s, m.sep_id] for s in seqs[i:i + self.BATCH]]
            width = max(len(s) for s in batch)
            ids = np.full((len(batch), width), m.pad_id, dtype=np.int64)
            mask = np.zeros((len(batch), width), dtype=np.int64)
            for r, s in enumerate(batch):
                ids[r, :len(s)] = s
                mask[r, :len(s)] = 1
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            logits = self.session.run(None, feed)[0]
            if m.output == "sigmoid1":
                probs.append(_sigmoid(logits))
            else:
                probs.append(_softmax_ai(logits, m.ai_index))
        return np.concatenate(probs) if probs else np.zeros(0)

    # ── Scorer API ──────────────────────────────────────────────────────────
    def score_document(self, text: str) -> tuple[float, int]:
        ids = self._ids(text)
        chunks = self._chunks(ids, self.meta.max_length - 2)
        p = self._run(chunks)
        # Weighted by chunk length: a 40-token tail must not count as much
        # as a full 510-token chunk.
        w = np.array([len(c) for c in chunks], dtype=np.float64)
        return float((p * w).sum() / w.sum()), len(chunks)

    def score_sentences(self, sentences: Sequence[str]) -> list[float]:
        seqs = [self._ids(s)[: self.SENTENCE_MAX_TOKENS - 2] for s in sentences]
        # Sort by length so each batch pads little; restore order after.
        order = sorted(range(len(seqs)), key=lambda k: len(seqs[k]))
        p = self._run([seqs[k] for k in order])
        out = [0.0] * len(seqs)
        for pos, k in enumerate(order):
            out[k] = float(p[pos])
        return out


class Registry:
    """Language → model. Loaded once at startup."""

    def __init__(self, models: list[Scorer]) -> None:
        self.models = models
        self.by_lang: dict[str, Scorer] = {}
        for model in models:
            for lang in model.languages:
                if lang in self.by_lang:
                    logger.warning(
                        "language %s served by both %s and %s; keeping the first",
                        lang, self.by_lang[lang].name, model.name,
                    )
                    continue
                self.by_lang[lang] = model

    def for_language(self, lang: str) -> Scorer | None:
        return self.by_lang.get(lang)

    @classmethod
    def from_dir(cls, root: Path, threads: int | None = None, languages: set[str] | None = None) -> "Registry":
        models: list[Scorer] = []
        if root.is_dir():
            for sub in sorted(p for p in root.iterdir() if p.is_dir()):
                if not (sub / META_FILE).exists():
                    continue
                try:
                    meta = ModelMeta.load(sub / META_FILE)
                    if not meta.extra.get("enabled", True):
                        continue
                    if languages is not None and not languages.intersection(meta.languages):
                        continue
                    backend = meta.extra.get("backend", "onnx")
                    if backend == "tfidf-linear-v1":
                        from detector.service.linear import LinearDetector
                        models.append(LinearDetector(sub, threads=threads))
                    elif backend == "profile-onnx-v1":
                        from detector.service.profile import ProfileOnnxDetector
                        models.append(ProfileOnnxDetector(sub, threads=threads))
                    elif backend == "stacked-ru-v1":
                        from detector.service.stacked import StackedRussianDetector
                        models.append(StackedRussianDetector(sub, threads=threads))
                    elif backend == "onnx" and (sub / "model.onnx").exists():
                        models.append(OnnxDetector(sub, threads=threads))
                    else:
                        continue
                    logger.info("loaded detector model %s", sub.name)
                except Exception:  # noqa: BLE001 — one bad model must not stop the rest
                    logger.exception("failed to load detector model %s", sub)
        return cls(models)


def default_threads() -> int | None:
    raw = os.environ.get("DETECTOR_THREADS")
    return int(raw) if raw else None
