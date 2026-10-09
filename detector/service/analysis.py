"""The /detect decision, independent of HTTP and of the concrete model."""
from __future__ import annotations

import os
from dataclasses import dataclass

from detector.service.engine import Registry
from detector.service.lang import MIXED as LANG_MIXED, UNKNOWN, detect_language, script_of
from detector.service.text import clean_text, split_sentences, truncate_words, word_count

TOO_SHORT = "too_short"
UNSUPPORTED = "unsupported"
LIKELY_HUMAN = "likely_human"
MIXED = "mixed"
LIKELY_AI = "likely_ai"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Limits:
    min_words: int = 80
    # Longer texts are scored on their first max_words words (a whole
    # coursework would otherwise hold a CPU for a minute); the response says so.
    max_words: int = 6000
    # A text with almost no spaces ("one word" of 200k characters) passes the
    # word cap and would become hundreds of 512-token chunks. ~10 characters
    # per word covers every real language here.
    max_chars: int = 60_000
    # A 3-word sentence carries no signal; it is returned unscored.
    sentence_min_words: int = 5
    max_sentences: int = 400

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            min_words=_env_int("DETECTOR_MIN_WORDS", cls.min_words),
            max_words=_env_int("DETECTOR_MAX_WORDS", cls.max_words),
            max_chars=_env_int("DETECTOR_MAX_CHARS", cls.max_chars),
            sentence_min_words=_env_int("DETECTOR_SENTENCE_MIN_WORDS", cls.sentence_min_words),
            max_sentences=_env_int("DETECTOR_MAX_SENTENCES", cls.max_sentences),
        )


def verdict_for(p: float, threshold: float, band: float) -> str:
    """Use the frozen corpus decision cut; this is not an authorship guarantee."""
    if p >= threshold:
        return LIKELY_AI
    if p >= threshold - band:
        return MIXED
    return LIKELY_HUMAN


def analyze(text: str, registry: Registry, limits: Limits = Limits(), *, explain: bool = False) -> dict:
    text = clean_text(text)
    n_words = word_count(text)
    lang = detect_language(text)
    model = registry.for_language(lang) if lang not in (UNKNOWN, LANG_MIXED) else None

    base = {
        "lang": lang,
        "supported": model is not None,
        "ai_probability": None,
        "human_similarity": None,
        "explanation": None,
        "authorship_confidence": None,
        "score_kind": "model_similarity_not_authorship_probability",
        "api_contract_version": 2,
        "decision_thresholds": {"ai_at_or_above": model.threshold, "human_below": model.threshold-model.mixed_band} if model else None,
        "verdict": None,
        "sentences": [],
        "model_version": model.version if model else None,
        "word_count": n_words,
        "truncated": False,
        "chunks": 0,
        "detail": None,
    }

    required_words = max(limits.min_words, getattr(model, "min_words", 0))
    if n_words < required_words:
        base["verdict"] = TOO_SHORT
        base["detail"] = f"text too short: {n_words} words, need at least {required_words}"
        return base
    if model is None:
        base["verdict"] = UNSUPPORTED
        base["detail"] = (
            "text mixes Latin and Cyrillic too much for one model" if lang == LANG_MIXED
            else f"no model for language '{lang}'"
        )
        return base

    text, truncated = truncate_words(text, limits.max_words)
    if len(text) > limits.max_chars:
        text, truncated = text[: limits.max_chars], True
    p_doc, n_chunks = model.score_document(text)
    if hasattr(model, "is_truncated"):
        truncated = truncated or model.is_truncated(text)

    sentences = split_sentences(text)
    doc_script = script_of(text)
    scorable = [
        i for i, s in enumerate(sentences)
        if word_count(s) >= max(limits.sentence_min_words, getattr(model, "sentence_min_words", 0), 150 if explain else 0)
        # a quoted sentence in the other script is not this model's language
        and script_of(s) in (doc_script, None)
    ][: limits.max_sentences]
    scores = model.score_sentences([sentences[i] for i in scorable]) if scorable else []
    by_index = dict(zip(scorable, scores))

    base.update(
        ai_probability=round(p_doc, 4),
        human_similarity=round(1-p_doc, 4),
        verdict=verdict_for(p_doc, model.threshold, model.mixed_band),
        sentences=[
            {"text": s, "ai_probability": round(by_index[i], 4) if i in by_index else None}
            for i, s in enumerate(sentences)
        ],
        truncated=truncated,
        chunks=n_chunks,
        detail=getattr(model, "detail", None),
    )
    if explain:
        from detector.service.explain import explain_parts
        base["explanation"] = explain_parts(text, model, lang, p_doc)
    return base
