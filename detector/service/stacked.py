"""Russian detector combining local corpus features with a pinned pretrained model."""
from __future__ import annotations
import math
from pathlib import Path
from typing import Sequence

from detector.service.engine import ModelMeta
from detector.service.linear import LinearDetector, sigmoid
from detector.service.profile import ProfileOnnxDetector

BACKEND = "stacked-ru-v1"


def logit(probability: float) -> float:
    p = min(1 - 1e-6, max(1e-6, probability))
    return math.log(p / (1 - p))


class StackedRussianDetector:
    min_words = 150
    sentence_min_words = 150

    def __init__(self, model_dir: Path, threads: int | None = None) -> None:
        self.dir = Path(model_dir)
        self.meta = ModelMeta.load(self.dir / "detector_meta.json")
        extra = self.meta.extra
        if extra.get("backend") != BACKEND or self.meta.languages != ["ru"]:
            raise ValueError("unsupported ensemble profile")
        self.name, self.version = self.meta.name, self.meta.version
        self.languages = list(self.meta.languages)
        self.threshold, self.mixed_band = self.meta.threshold, self.meta.mixed_band
        self.coefficients = [float(value) for value in extra["coefficients"]]
        self.intercept = float(extra["intercept"])
        if len(self.coefficients) != 2 or not all(
                math.isfinite(value) for value in [*self.coefficients, self.intercept]):
            raise ValueError("invalid ensemble coefficients")
        if not .5 <= self.threshold <= 1 or not 0 <= self.mixed_band < self.threshold:
            raise ValueError("invalid ensemble thresholds")
        self.linear = LinearDetector(self.dir / "linear")
        self.neural = ProfileOnnxDetector(self.dir / "neural", threads)
        self.detail = (
            "Offline statistical estimate, not authorship proof. "
            "Russian ensemble evaluated on local corpus and CoAT/LLMTrace subsets. "
            "The neural component reads only the first 512 tokens; "
            "school essay accuracy and a population false-positive bound are not established."
        )

    def score_document(self, text: str) -> tuple[float, int]:
        linear, n_linear = self.linear.score_document(text)
        neural, n_neural = self.neural.score_document(text)
        margin = (self.intercept + self.coefficients[0] * logit(linear)
                  + self.coefficients[1] * logit(neural))
        return sigmoid(margin), max(n_linear, n_neural)

    def is_truncated(self, text: str) -> bool:
        return self.neural.is_truncated(text)

    def score_sentences(self, sentences: Sequence[str]) -> list[float]:
        return [self.score_document(sentence)[0] for sentence in sentences]