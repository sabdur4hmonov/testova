"""Use a model publisher's pinned ONNX preprocessing profile without torch."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from typing import Sequence

from detector.service.engine import OnnxDetector

BACKEND = "profile-onnx-v1"


class ProfileOnnxDetector(OnnxDetector):
    BATCH = 4
    min_words = 150
    sentence_min_words = 150

    def __init__(self, model_dir: Path, threads: int | None = None) -> None:
        model_dir = Path(model_dir)
        meta = json.loads((model_dir / "detector_meta.json").read_text(encoding="utf8"))
        for name, expected in meta["artifact_sha256"].items():
            if name not in ("model.onnx", "tokenizer.json", "inference.json"):
                raise ValueError("unexpected ONNX artifact")
            digest = hashlib.sha256()
            with (model_dir / name).open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError("ONNX model checksum mismatch")
        self.profile = json.loads((model_dir / "inference.json").read_text(encoding="utf8"))
        if self.profile["version"] != 1 or self.profile["labels"] != ["human", "ai"]:
            raise ValueError("unsupported inference profile")
        if self.profile["file"] != "model.onnx" or not 1 <= self.profile["max_windows"] <= 16:
            raise ValueError("invalid model window profile")
        self.normalization = [(re.compile(rule["pattern"], re.MULTILINE), rule["replacement"])
                              for rule in self.profile["normalize"]]
        super().__init__(model_dir, threads)
        self.detail = (
            "Offline estimate using a published Russian model. Not proof of authorship. "
            "Only the first 512 tokens are evaluated; short sentence scores are omitted. "
            "School essay accuracy and a population false-positive bound are not established."
        )

    def _ids(self, text: str) -> list[int]:
        normalized = text[:self.profile["max_chars"]]
        for pattern, replacement in self.normalization:
            normalized = pattern.sub(replacement, normalized)
        return super()._ids(normalized.strip())

    def score_document(self, text: str) -> tuple[float, int]:
        ids = self._ids(text)
        body = self.profile.get("read_length", self.profile["max_length"]) - 2
        chunks = [ids[start:start + body] for start in range(0, max(1, len(ids)), body)]
        chunks = chunks[:self.profile["max_windows"]]
        return float(self._run(chunks).mean()), len(chunks)

    def is_truncated(self, text: str) -> bool:
        body = self.profile.get("read_length", self.profile["max_length"]) - 2
        return len(self._ids(text)) > body * self.profile["max_windows"]

    def score_sentences(self, sentences: Sequence[str]) -> list[float]:
        return [self.score_document(sentence)[0] for sentence in sentences]