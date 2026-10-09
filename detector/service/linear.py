"""Small offline TF-IDF classifiers; inference uses only NumPy and the standard library."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Sequence

import numpy as np

from detector.service.engine import ModelMeta
from detector.service.lang import words
from detector.service.text import clean_text

BACKEND = "tfidf-linear-v1"
_APOSTROPHES = re.compile("['ʻʼ‘’\x60]")
_URL = re.compile(r"https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.\w+")
FEATURE_VERSION = 1


def feature_tokens(text: str) -> list[str]:
    """Training and inference share this exact analyzer; no metadata is a feature."""
    normalized = _APOSTROPHES.sub("'", clean_text(_URL.sub(" ", text)).lower())
    tokens = words(normalized)
    result = ["w1:" + token for token in tokens]
    result.extend("w2:" + a + " " + b for a, b in zip(tokens, tokens[1:]))
    for token in tokens:
        padded = " " + token + " "
        for n in (3, 4, 5):
            result.extend(f"c{n}:" + padded[i:i + n] for i in range(len(padded) - n + 1))
    return result


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-min(value, 700)))
    exponential = math.exp(max(value, -700))
    return exponential / (1.0 + exponential)


class LinearDetector:
    """No pickle, network, external API or scikit-learn dependency in serving."""

    sentence_min_words = 150
    min_words = 150

    def __init__(self, model_dir: Path, threads: int | None = None) -> None:
        self.dir = Path(model_dir)
        self.meta = ModelMeta.load(self.dir / "detector_meta.json")
        extra = self.meta.extra
        if extra.get("backend") != BACKEND:
            raise ValueError("unsupported linear backend")
        self.name = self.meta.name
        self.version = self.meta.version
        self.languages = list(self.meta.languages)
        self.threshold = float(self.meta.threshold)
        self.mixed_band = float(self.meta.mixed_band)
        if not (0.5 <= self.threshold <= 1 and 0 <= self.mixed_band < self.threshold):
            raise ValueError("invalid decision thresholds")
        for name, expected in extra["artifact_sha256"].items():
            if name not in ("vectorizer.json", "weights.npz"):
                raise ValueError("unexpected model artifact")
            artifact = self.dir / name
            if artifact.stat().st_size > 20_000_000:
                raise ValueError("linear model artifact too large")
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
                raise ValueError("model artifact checksum mismatch")
        config = json.loads((self.dir / "vectorizer.json").read_text(encoding="utf8"))
        if config["feature_version"] != FEATURE_VERSION:
            raise ValueError("feature version mismatch")
        features = config["features"]
        if not 1 <= len(features) <= 100_000 or len(set(features)) != len(features):
            raise ValueError("invalid feature vocabulary")
        self.vocabulary = {feature: index for index, feature in enumerate(features)}
        with np.load(self.dir / "weights.npz", allow_pickle=False) as archive:
            self.idf = archive["idf"].astype(np.float64)
            self.weights = archive["weights"].astype(np.float64)
            self.intercept = float(archive["intercept"].item())
            self.calibration_a = float(archive["calibration_a"].item())
            self.calibration_b = float(archive["calibration_b"].item())
        if self.idf.shape != (len(features),) or self.weights.shape != self.idf.shape:
            raise ValueError("model dimensions do not match")
        if not np.isfinite(self.idf).all() or not np.isfinite(self.weights).all():
            raise ValueError("non-finite model arrays")
        if not all(math.isfinite(x) for x in
                   (self.intercept, self.calibration_a, self.calibration_b)):
            raise ValueError("non-finite calibration")
        if self.calibration_a <= 0 or np.any(self.idf <= 0):
            raise ValueError("invalid calibration or IDF")
        self.detail = (
            "Offline statistical estimate, not proof of authorship. "
            "Validated on the listed corpus and Gemini generators; "
            "school essays, other generators and edited texts are not independently validated. "
            "Sentence scores require at least 150 words."
        )

    def decision_function(self, text: str) -> float:
        counts = Counter(feature_tokens(text))
        pairs = [(self.vocabulary[token], count) for token, count in counts.items()
                 if token in self.vocabulary]
        if not pairs:
            return self.intercept
        indices = np.fromiter((index for index, _ in pairs), dtype=np.int64)
        values = np.array([1.0 + math.log(count) for _, count in pairs]) * self.idf[indices]
        norm = float(np.linalg.norm(values))
        return self.intercept + (float(np.dot(values, self.weights[indices])) / norm if norm else 0.0)

    def probability(self, text: str) -> float:
        return sigmoid(self.calibration_a * self.decision_function(text) + self.calibration_b)

    def score_document(self, text: str) -> tuple[float, int]:
        # Whole training chunks (150-350 words) remain intact. Split longer input
        # without dropping a short tail; aggregate by word count.
        matches = list(re.finditer(r"[^\W\d_]+(?:[\x27ʻʼ‘’\x60][^\W\d_]+)*", text))
        if len(matches) <= 350:
            return self.probability(text), 1
        spans = []
        start = 0
        while start < len(matches):
            end = min(start + 300, len(matches))
            if len(matches) - end < 80:
                end = len(matches)
            spans.append((text[matches[start].start():matches[end - 1].end()], end - start))
            start = end
        total = sum(count for _, count in spans)
        return sum(self.probability(chunk) * count for chunk, count in spans) / total, len(spans)

    def score_sentences(self, sentences: Sequence[str]) -> list[float]:
        # analysis filters out short sentences before calling this scorer.
        return [self.probability(sentence) for sentence in sentences]