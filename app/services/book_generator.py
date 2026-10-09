"""
Question generation from a book's saved theme text (Phase 3), with CP14's
grounding + media enforcement.

Book ANALYSIS (Phase 1) runs on the paid key with thinking OFF, because
finding a table of contents is a structural read. GENERATION is the opposite:
it is the reasoning step, so it runs with thinking ON, via
gemini_rest.call_generation. CP13 originally built this to try a free key
first and fall back to paid on 429; CP14 measured that live against the
project's real keys and removed it — see gemini_rest's module docstring for
why (404 on the configured model, intermittent 503 on the free-tier models
that DID accept it, neither of which the 429-only fallback caught). Paid key,
always, is what actually ships.

ONE CALL PER THEME, not one per request. Three reasons, in order of weight:
  1. GROUNDING is per-theme — an answer must come from THAT theme's text, and
     a single call over ten themes invites the model to blend them.
  2. max_output_tokens is 8192; a 50-question request would risk truncation
     mid-array, losing the whole batch.
  3. One theme's failure then costs one theme, not the request.

Measured on the real 6th-grade biology book: themes run 2.7k-6.9k characters
(~670-1720 tokens), so a per-theme prompt is comfortably small.

CP14 GROUNDING ENFORCEMENT — measured, not assumed, against 25 real questions
generated for 5 real themes on the shipping model (gemini-2.5-flash, paid
key). Two findings drove the design:
  * Strict verbatim-substring grounding is far too strict on THIS model: it
    would have DROPPED half of all multiple-choice answers and 80% of written
    ones, and every single one inspected was in fact a genuine, correctly-
    grounded answer — just paraphrased or inflected ("Прямую, скученную" for
    the book's "прямые, скученные"). A stem-based overlap check (5-char
    stem, casefolded, de-hyphenated via normalize_for_grounding) fixes this:
    scored against a NEGATIVE CONTROL (each answer checked against the OTHER
    four themes' text — 100 real-but-unsupported pairs), threshold 0.90 keeps
    100% of the genuinely grounded answers while admitting only 6% of the
    adversarial ones.
  * Distractors are held to a MAJORITY standard, not all-or-nothing: a
    quantity question's wrong options ("10 кг" against a stated "25 кг") are
    SUPPOSED to be absent from the source, so requiring every distractor to be
    grounded would penalize good questions for doing their job. Numeric/
    unit-only distractors have no scoreable content token and are exempt by
    construction (see _content_tokens' length-3 floor).

Policy (approved): check → if it fails, ONE regeneration attempt, batched for
the whole theme (not one call per failing question — matches this module's
call-economy stance) → recheck each replacement → still-failing ones are
dropped, never guessed at or included anyway.

CP15 — generate_for_theme returns GenerationResult, not a bare list. A bare
[] could not distinguish "this theme legitimately supports zero questions"
from "a Gemini call (or the validity check itself) raised" — exactly how the
CP13 free-key 404 stayed invisible until it was live-measured. errored=True
only when something actually raised; a regeneration that runs cleanly but is
still ungrounded is the drop policy working as designed, not an error.
"""
from __future__ import annotations

from app.services.finish_policy import require_usable_finish

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.config import settings
from app.services import gemini_rest
from app.utils.logging import get_logger

logger = get_logger(__name__)

# One Gemini call per theme; bound the reply so a runaway generation cannot
# stall a teacher who is waiting.
GENERATION_TIMEOUT = 180
MAX_OUTPUT_TOKENS = 8192

# CP26: after CP14's initial call + its one regeneration attempt, a theme may
# STILL be short of the target — most often because the model simply returned
# fewer questions than asked (the common under-delivery case), or because
# dedup/format checks dropped some. This is how many EXTRA batched calls we make
# to chase the remaining shortfall before giving up on that theme. Ceiling is
# hard and small (R=2) so the cost is bounded; a round that adds nothing new
# early-exits, since it means the section is genuinely out of gradable material
# and asking again would only spend a request to reinvent the same misses.
BACKFILL_MAX_ROUNDS = 2

# AUDIT B-65. One call per THEME is right for grounding, but a page-range book
# can legitimately have ONE theme, and handle_generate_count accepts up to 100
# questions - so all 100 went into a single call with nothing chunking them.
#
# maxOutputTokens bounds THINKING + OUTPUT together (measured: thinking budgets
# 2048/512/0 all truncate at sum ~8,180), so the effective content budget is
# the cap MINUS whatever thinking consumes: ~6,144 at today's 8192 cap and
# 2048 thinking budget. At a MEASURED ~100 output tokens per question (max
# 124 observed), the largest safe chunk is ~49. 45 leaves ~10% margin
# (45 x 124 = 5,580 < 6,144).
#
# Raising maxOutputTokens instead was tested and REJECTED: at 16384 the model
# still truncated, and at 32768 it returned 151 questions for a 100 ask -
# paying for over-generation rather than complying.
#
# COST NOTE, measured and not hidden: chunking is NOT free. Each chunk pays its
# own thinking budget and re-sends the prompt (~86% of which does hit the
# implicit prefix cache). One clean paired run of a 100-question ask measured
# $0.05274 unchunked vs $0.06302 at a ceiling of 30 - about +20%. The ceiling
# is 45 rather than 30 to cut the number of chunks (3 instead of 4 on a
# 100-ask) and with it that per-chunk overhead.
#
# What the fix buys is DETERMINISM: without it the first call truncates and the
# delivered count depends on how well the CP26 backfill happens to recover
# (observed 94/100 and 100/100 on different runs of the same request). Asks at
# or below this ceiling take exactly one call and are unchanged.
MAX_QUESTIONS_PER_CALL = 45

# AUDIT C-2. How much of the already-accepted set a follow-up call is told
# about. The block exists to stop the NEXT call re-deriving what the last one
# produced, so when a theme has accumulated more than this the MOST RECENT
# stems are the ones kept. Bounded on both axes because a theme can legitimately
# reach 50+ accepted questions and the block rides on top of the full theme
# text on every regen/backfill round.
AVOID_MAX_STEMS = 30
AVOID_STEM_CHARS = 160


# ── OPT4: safe theme-text compression (fewer input tokens, ZERO fact loss) ───
#
# Removes ONLY structural noise — collapsed whitespace, blank-line runs, running
# headers/footers, and page-number lines that form a sequence. Never removes a
# factual line: a numbered list item ("1. ..."), a short fact, a bare numeric
# answer that stands alone, or an equation number all survive. Applied to the
# text sent to GENERATION only; grounding still runs against the full text, so
# recall can never be hurt by a stripped line.

_WS_RUN = re.compile(r"[ \t]+")
_BARE_INT = re.compile(r"^\d{1,4}$")


def compress_theme_text(text: str) -> str:
    """Shrink a theme's text for the generation prompt without dropping facts.

    Safe by construction — the only removals are:
      * runs of intra-line whitespace collapsed to one space;
      * runs of blank lines collapsed to one;
      * running headers/footers — a SHORT line (<=5 words) that repeats
        IDENTICALLY 3+ times and is not a full sentence (structural, never
        prose);
      * page numbers — a BARE integer on its own line, but ONLY when it is part
        of a monotonic near-sequential RUN of 3+ such lines. A lone bare number
        (a numeric answer, an equation number, a numbered list item — which is
        never bare, it has text after the dot) is always kept.
    """
    if not text:
        return text
    lines = [_WS_RUN.sub(" ", ln).strip() for ln in text.split("\n")]

    from collections import Counter
    freq = Counter(
        ln for ln in lines
        if ln and len(ln.split()) <= 5 and ln[-1] not in ".!?"
    )
    headers = {ln for ln, c in freq.items() if c >= 3}

    # Bare-integer lines that form a monotonic run of >=3 (consecutive values
    # differing by 1-2) are page numbers; isolated bare integers are NOT.
    bare = [(i, int(lines[i])) for i, ln in enumerate(lines) if _BARE_INT.match(ln)]
    page_num_idx: set[int] = set()
    if len(bare) >= 3:
        run = [bare[0]]
        for prev, cur in zip(bare, bare[1:]):
            if 0 < cur[1] - prev[1] <= 2:
                run.append(cur)
            else:
                if len(run) >= 3:
                    page_num_idx.update(i for i, _ in run)
                run = [cur]
        if len(run) >= 3:
            page_num_idx.update(i for i, _ in run)

    out: list[str] = []
    prev_blank = False
    for i, ln in enumerate(lines):
        if i in page_num_idx or ln in headers:
            continue
        if not ln:
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(ln)
    return "\n".join(out).strip()


# ── Distributing questions across the selected themes ────────────────────────

def distribute_questions(
    total: int, n_themes: int, weights: list[int] | None = None,
) -> list[int]:
    """Split `total` questions across `n_themes`.

    WITHOUT `weights`, as evenly as possible: the remainder goes to the EARLIEST
    themes, deterministically — so the same request always produces the same
    split, and no theme ever carries two more than another. With fewer questions
    than themes the trailing themes get 0, which is unavoidable and must not be
    papered over. This is the original behaviour, byte-identical, and it is what
    every caller with no page information still gets.

    WITH `weights` (each theme's PAGE SPAN), proportionally, by largest-remainder
    (Hamilton) apportionment:

        exact_i  = total * w_i / sum(w)
        base_i   = floor(exact_i)
        leftover = total - sum(base_i)  ->  to the largest fractional parts,
                                            ties broken by order, earliest first

    Ties-to-earliest is the same rule the even split uses for its remainder, so
    the function stays deterministic in exactly the way it always was.

    WHY: an even split is a split by how the teacher happened to punctuate their
    input. "1-15, 40-42" gave a 15-page range and a 3-page one the same number
    of questions, and across two books the one typed as more ranges quietly won
    the larger share of the test. Page span is the thing the questions actually
    come from.

    NO PER-THEME MINIMUM: a 1-page range beside a 40-page one can receive zero.
    A floor of 1 would break the exact-sum property and over-weight tiny ranges.
    The even split already produced zeros; weighting moves them onto the
    SMALLEST themes rather than the LAST ones.

    A degenerate `weights` (wrong length, empty, non-positive sum, or any
    negative entry) falls back to the even split rather than raising — a bad
    weight list must never fail a teacher's generation, and the even split is
    the behaviour that was always correct.
    """
    if n_themes <= 0:
        return []
    total = max(0, total)

    usable = (
        weights is not None
        and len(weights) == n_themes
        and all(w >= 0 for w in weights)
        and sum(weights) > 0
    )
    if not usable:
        base, extra = divmod(total, n_themes)
        return [base + (1 if i < extra else 0) for i in range(n_themes)]

    weight_sum = sum(weights)
    exact = [total * w / weight_sum for w in weights]
    out = [int(e) for e in exact]
    leftover = total - sum(out)
    # Largest fractional part first; index ASCENDING breaks ties, so the
    # earliest theme wins exactly as it does in the even split.
    order = sorted(range(n_themes), key=lambda i: (-(exact[i] - out[i]), i))
    for i in order[:leftover]:
        out[i] += 1
    return out


# ── Grounding normalizer ─────────────────────────────────────────────────────
#
# MEASURED, not assumed: the real textbook hyphenates at line breaks once per
# ~141 characters ("по-\nзнакомитесь"). A grounding check that compared raw
# strings would therefore call almost every correct answer ungrounded, and the
# CP14 enforcement built on it would throw away good questions wholesale.
#
# Only hyphen-IMMEDIATELY-BEFORE-newline is a typographic break. A hyphen
# anywhere else is real punctuation ("Ас-Сайдана", "фит-тибб") and is kept.

_HYPHEN_BREAK = re.compile(r"[-­‐‑]\s*\n\s*")
_WHITESPACE = re.compile(r"\s+")


def normalize_for_grounding(text: str | None) -> str:
    """Casefolded, de-hyphenated, whitespace-collapsed form for substring
    comparison. Used by the CP14 grounding check on BOTH sides, so even an
    imperfect fold is harmless — it is the same function either way."""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", str(text))
    out = _HYPHEN_BREAK.sub("", out)          # rejoin words split across lines
    out = _WHITESPACE.sub(" ", out)
    return out.strip().casefold()


# ── Grounding predicates (CP14) ───────────────────────────────────────────────
#
# See the module docstring for the measurement behind these numbers. In short:
# strict substring matching is too strict for this model's paraphrasing and
# Russian inflection ("Прямую" for the book's "прямые"); a stem-overlap check
# is not. Both thresholds were chosen against a REAL negative control (each
# answer scored against a THEME IT WASN'T GENERATED FROM), not guessed.

_WORD = re.compile(r"\w+", re.UNICODE)
_STEM_LEN = 5             # chars kept per token before comparing
_MIN_TOKEN_LEN = 3        # shorter tokens (units like "кг", "м") are not scored
_ANSWER_OVERLAP_THRESHOLD = 0.90
_DISTRACTOR_OVERLAP_THRESHOLD = 0.5   # per-distractor: fraction of ITS tokens grounded
_DISTRACTOR_MAJORITY_THRESHOLD = 0.5  # fraction of a question's distractors that must pass


def _content_tokens(text: str) -> list[str]:
    """Casefolded, de-hyphenated tokens with the short units filtered out.

    A token shorter than _MIN_TOKEN_LEN ("кг", "м", "10") carries no content
    to check — that is what lets a pure-number-or-unit distractor ("10 кг"
    against a stated "25 кг") pass by construction: it is SUPPOSED to be
    absent from the source, not evidence of invention.
    """
    norm = normalize_for_grounding(text)
    return [w for w in _WORD.findall(norm) if len(w) >= _MIN_TOKEN_LEN]


def _stem_set(tokens: list[str]) -> set[str]:
    return {w[:_STEM_LEN] for w in tokens}


def _token_overlap(tokens: list[str], hay_stems: set[str]) -> float:
    if not tokens:
        return 0.0
    return sum(1 for w in tokens if w[:_STEM_LEN] in hay_stems) / len(tokens)


def is_answer_grounded(answer: str, theme_text: str) -> bool:
    """True if `answer`'s content is supported by `theme_text`.

    A pure numeric/unit answer ("3-4 м") has no scoreable token — the overlap
    ratio would be a divide-by-zero fiction — so it falls back to the
    verbatim (normalized) substring check, which is exactly right for a
    number: there is no inflection to paraphrase.
    """
    tokens = _content_tokens(answer)
    if not tokens:
        return normalize_for_grounding(answer) in normalize_for_grounding(theme_text)
    hay_stems = _stem_set(_content_tokens(theme_text))
    return _token_overlap(tokens, hay_stems) >= _ANSWER_OVERLAP_THRESHOLD


def answer_is_short(answer: str) -> bool:
    """Is this open-answer reference answer short enough to actually GRADE?

    Grading is checker.is_correct — an EXACT match after normalization, with
    equivalences only for single letters and plain numbers. Nothing longer than
    a short phrase can survive that against a student's handwriting, so a
    multi-sentence reference answer is not a hard question, it is an ungradeable
    one.

    Two ceilings, and the WORD one is the filter that matters. The CHAR one is a
    backstop for a pathological three-"word" answer rather than a second filter:
    Uzbek is agglutinative, so a word-count-only rule behaves differently across
    the bot's languages. See MAX_WRITTEN_ANSWER_WORDS/CHARS for the measurements.

    Blank is False: it is not a short answer, it is a missing one (the parser
    already drops it earlier; this must never report it as passing).
    """
    text = " ".join(str(answer or "").split())
    if not text:
        return False
    return (
        len(text.split()) <= settings.MAX_WRITTEN_ANSWER_WORDS
        and len(text) <= settings.MAX_WRITTEN_ANSWER_CHARS
    )


def distractors_pass(options: dict[str, str], correct_label: str, theme_text: str) -> bool:
    """True if a MAJORITY of the wrong options are contextually grounded.

    Not all-or-nothing: a distractor for a quantity question is CORRECT to
    write as an ungrounded number ("10 кг" vs. a stated "25 кг" — see rule 2
    of the prompt), and an all-or-nothing rule would punish a question for
    doing that job well. Numeric/unit-only distractors are exempt from the
    per-distractor check the same way a numeric answer is (see
    is_answer_grounded) — they have no scoreable token to overlap on.
    """
    wrong = [v for k, v in options.items() if k != correct_label]
    if not wrong:
        return True
    hay_stems = _stem_set(_content_tokens(theme_text))
    grounded = 0
    for value in wrong:
        tokens = _content_tokens(value)
        if not tokens or _token_overlap(tokens, hay_stems) >= _DISTRACTOR_OVERLAP_THRESHOLD:
            grounded += 1
    return (grounded / len(wrong)) >= _DISTRACTOR_MAJORITY_THRESHOLD


# ── CP27: local, deterministic quality checks (NO Gemini call) ────────────────
#
# Two cheap checks that need no model judgment, so they run as pure code:
#   * distractor FORMAT — an MC question whose options are malformed
#     (a repeated option, a blank, or one answer that dwarfs every distractor)
#     is a correctness/quality defect the grounding gate does not catch. Folded
#     into _question_is_valid so a failure rides the SAME check → regen → drop
#     path CP14 already runs, and CP26's backfill then refills the slot with a
#     well-formed question instead of shipping the broken one.
#   * near-DUPLICATE removal — two questions that test the same thing in almost
#     the same words are repetition, not coverage. dedupe_questions drops the
#     later of any such pair, applied per-theme (so CP26 backfill refills the
#     dropped slot) and once more across themes at aggregation.
#
# The near-duplicate bar is deliberately the SAME high-confidence one
# ai_analyzer.find_near_duplicates already reports on and the team already
# trusts — identical normalized option set AND stem similarity >= 0.90 — except
# here we ACT on it by dropping, which is safe precisely because that bar was
# chosen to avoid collapsing genuinely different questions.

_DUP_STEM_RATIO = 0.90


def _norm_option_set(options: dict[str, str]) -> tuple[str, ...]:
    return tuple(sorted(normalize_for_grounding(v) for v in options.values()))


def distractors_well_formed(options: dict[str, str], correct_label: str) -> bool:
    """True if an MC question's options are structurally sound: none blank, and
    no two identical (a repeated option is an ambiguous, effectively-two-correct
    question). Pure, deterministic.

    A LENGTH-outlier check ("the answer dwarfs the distractors") was prototyped
    here and DROPPED: measured against the real stored questions it rejected a
    legitimate one whose correct answer is genuinely a longer scientific term
    ("дезоксирибонуклеин кислотаси") against short single-word distractors — a
    false positive on good content. Length MATCHING is a real quality signal but
    needs a non-destructive mechanism (a rewrite/critique pass), not a hard drop
    that punishes a correct answer for being a fuller phrase; deferred to the
    post-deploy quality phase where it can be tuned against real generated data.
    """
    norm = [normalize_for_grounding(v) for v in options.values()]
    if any(not n for n in norm):
        return False                       # a blank option (defensive)
    if len(set(norm)) != len(norm):
        return False                       # a repeated option value
    return True


def _stem_similar(a: str, b: str) -> bool:
    return bool(a) and bool(b) and SequenceMatcher(None, a, b).ratio() >= _DUP_STEM_RATIO


def _is_near_duplicate(question: dict[str, Any], accepted: list[dict[str, Any]]) -> bool:
    """True if `question` repeats one already in `accepted`. MC: identical
    normalized option set AND a >=0.90-similar stem. Written: identical
    normalized answer AND a >=0.90-similar stem. Two questions that share a stem
    but differ in options/answer are siblings, not duplicates, and are kept."""
    q_stem = normalize_for_grounding(question["question_text"])
    if not q_stem:
        return False
    if question["is_open_ended"]:
        q_ans = normalize_for_grounding(question["correct_answers"][0])
        return any(
            a["is_open_ended"]
            and normalize_for_grounding(a["correct_answers"][0]) == q_ans
            and _stem_similar(q_stem, normalize_for_grounding(a["question_text"]))
            for a in accepted
        )
    q_opts = _norm_option_set(question["options"])
    return any(
        not a["is_open_ended"]
        and _norm_option_set(a["options"]) == q_opts
        and _stem_similar(q_stem, normalize_for_grounding(a["question_text"]))
        for a in accepted
    )


def dedupe_indices(questions: list[dict[str, Any]]) -> list[int]:
    """Indices of the questions dedupe_questions keeps, in order. The index form
    is what trim_to_requested tracks, so nothing depends on object identity."""
    kept_idx: list[int] = []
    kept: list[dict[str, Any]] = []
    for i, q in enumerate(questions):
        if not _is_near_duplicate(q, kept):
            kept_idx.append(i)
            kept.append(q)
    return kept_idx


def dedupe_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the later of any near-duplicate pair, preserving order. O(n^2) over
    short stems — n is a test's question count, so this is cheap."""
    return [questions[i] for i in dedupe_indices(questions)]


# ── Exact requested count per question type ───────────────────────────────────
#
# Nothing used to trim an over-delivery: generate_for_theme only ever ADDS
# (regrounding, backfill), and neither _run_generation, validate_questions nor
# the save path caps the count. MEASURED in the Step 2 A/B (2026-09-21): a
# 45-question ask came back as 55, and at the shipped budget 4 of 18 tests had
# a type mix other than requested while the total looked right (8/9 written +
# 37/36 multiple-choice), which the total-only shortfall footer cannot show.
#
# trim_to_requested keeps EXACTLY each type's target, never compensates one
# type with the other, and keeps every theme's page-weighted allocation before
# letting one theme's surplus fill another theme's shortfall of the SAME type.
# Pure: no Gemini call, no I/O, deterministic. Called by _run_generation after
# the per-theme validate_questions pass and before numbering.

# Stem similarity below this is shared function words, not a repeated concept,
# so it scores as no redundancy and the next victim key decides. Above it the
# score is floored to 0.1 steps, so near-equal scores tie instead of a 0.01
# difference deciding.
_REDUNDANCY_FLOOR = 0.6
_TYPES = ("mc", "written")


@dataclass
class TrimResult:
    questions: list[dict[str, Any]]
    stats: dict[str, Any] = field(default_factory=dict)
    changed: bool = False


def requested_type_targets(
    total: int, n_written_total: int, n_themes: int, weights: list[int] | None = None,
) -> list[tuple[int, int]]:
    """Per theme (multiple-choice, written) targets, exactly as _run_generation
    allots them: distribute_questions for both counts, then the per-theme
    written <= questions clamp _gen_one_theme applies."""
    counts = distribute_questions(total, n_themes, weights=weights)
    written = distribute_questions(n_written_total, n_themes, weights=weights)
    return [(n - min(nw, n), min(nw, n)) for n, nw in zip(counts, written)]


def _trim_type(question: dict[str, Any]) -> str | None:
    """The type a question counts as: "mc", "written", or None when it is an
    open question whose reference answer breaks the open-answer rule (the same
    answer_is_short check _question_is_valid applies). None counts as neither,
    so it is dropped rather than filling a slot it cannot grade. Covers an MC
    that validate_questions turned into an open question."""
    if not question.get("is_open_ended"):
        return "mc"
    answers = question.get("correct_answers") or []
    return "written" if answers and answer_is_short(str(answers[0])) else None


def _trim_answer_text(question: dict[str, Any]) -> str:
    if question.get("is_open_ended"):
        return str((question.get("correct_answers") or [""])[0] or "")
    return str((question.get("options") or {}).get(question.get("correct_answer"), "") or "")


def trim_to_requested(
    theme_questions: list[list[dict[str, Any]]],
    targets: list[tuple[int, int]],
    theme_texts: list[str],
) -> TrimResult:
    """Keep exactly each theme's (multiple-choice, written) target.

    Order of work: cross-theme dedup (dedupe_indices, tracked by index); drop
    questions that count as neither type; stage 1 trims each theme's excess of
    each type down to its target; stage 2 fills a same-type shortfall in any
    theme from the other themes' surplus of that type; what remains is dropped.
    A type at or below its target is never touched.

    Victims, one at a time and re-scored after each: the most redundant stem
    (highest similarity to another kept question of the same theme), then the
    weakest answer grounding against the theme text, then the lowest sha256 of
    normalized stem|answer. Stage-2 fillers take the inverse order, scored
    against the whole test. Survivors keep their original order.
    """
    flat: list[dict[str, Any]] = []
    theme_of: list[int] = []
    for ti, qs in enumerate(theme_questions):
        flat.extend(qs)
        theme_of.extend([ti] * len(qs))

    types = [_trim_type(q) for q in flat]
    before = {t: sum(1 for x in types if x == t) for t in _TYPES}
    after_dedup = set(dedupe_indices(flat))
    ineligible = {i for i in after_dedup if types[i] is None}
    alive = after_dedup - ineligible

    stems = [normalize_for_grounding(q.get("question_text")) for q in flat]
    hashes = [
        hashlib.sha256(
            f"{stems[i]}|{normalize_for_grounding(_trim_answer_text(q))}".encode("utf-8")
        ).hexdigest()
        for i, q in enumerate(flat)
    ]
    hay = [_stem_set(_content_tokens(text or "")) for text in theme_texts]

    def grounding(i: int) -> float:
        if flat[i].get("img_index") is not None:
            return 1.0  # grounded in its figure; text checks do not apply
        tokens = _content_tokens(_trim_answer_text(flat[i]))
        return _token_overlap(tokens, hay[theme_of[i]]) if tokens else 1.0

    ratios: dict[tuple[int, int], float] = {}

    def ratio(i: int, j: int) -> float:
        key = (i, j) if i < j else (j, i)
        if key not in ratios:
            a, b = stems[i], stems[j]
            ratios[key] = SequenceMatcher(None, a, b).ratio() if a and b else 0.0
        return ratios[key]

    def redundancy(i: int, pool: set[int]) -> float:
        best = max((ratio(i, j) for j in pool if j != i), default=0.0)
        return 0.0 if best < _REDUNDANCY_FLOOR else math.floor(best * 10) / 10

    def keep_key(i: int, pool: set[int]) -> tuple:
        # Smallest = dropped first; largest = kept first as a stage-2 filler.
        return (-redundancy(i, pool), grounding(i), hashes[i])

    def target(ti: int, t: str) -> int:
        return targets[ti][0] if t == "mc" else targets[ti][1]

    def alive_of(ti: int, t: str) -> list[int]:
        return [i for i in alive if theme_of[i] == ti and types[i] == t]

    surplus: dict[str, list[int]] = {t: [] for t in _TYPES}
    for ti in range(len(theme_questions)):
        for t in _TYPES:
            members = alive_of(ti, t)
            for _ in range(len(members) - target(ti, t)):
                pool = {i for i in alive if theme_of[i] == ti}
                victim = min(members, key=lambda i: keep_key(i, pool))
                members.remove(victim)
                alive.discard(victim)
                surplus[t].append(victim)

    filled = {t: 0 for t in _TYPES}
    for t in _TYPES:
        need = sum(max(0, target(ti, t) - len(alive_of(ti, t)))
                   for ti in range(len(theme_questions)))
        while need > 0 and surplus[t]:
            filler = max(surplus[t], key=lambda i: keep_key(i, alive))
            surplus[t].remove(filler)
            alive.add(filler)
            filled[t] += 1
            need -= 1

    kept_idx = sorted(alive)
    kept = [flat[i] for i in kept_idx]
    requested = {t: sum(target(ti, t) for ti in range(len(theme_questions))) for t in _TYPES}
    after = {t: sum(1 for i in kept_idx if types[i] == t) for t in _TYPES}
    stats = {
        "requested": requested,
        "before": before,
        "after": after,
        "dropped": {t: len(surplus[t]) for t in _TYPES},
        "filled": filled,
        "shortfall": {t: max(0, requested[t] - after[t]) for t in _TYPES},
        "dedup_dropped": len(flat) - len(after_dedup),
        "ineligible": len(ineligible),
        "per_theme": [
            {"target": list(targets[ti]),
             "after": [sum(1 for i in kept_idx if theme_of[i] == ti and types[i] == t)
                       for t in _TYPES]}
            for ti in range(len(theme_questions))
        ],
    }
    return TrimResult(questions=kept, stats=stats, changed=len(kept) != len(flat))


# ── CP29b: ambiguity + concept-coverage critique (guard-gated, default off) ───
#
# ONE Gemini call per theme (thinking ON — this is judgment, not a structural
# read) reviews the already-grounded, already-deduped set and marks each question
# keep/drop for two defects neither the grounding gate nor the local dedup can
# see:
#   * AMBIGUITY (item 3) — a well-prepared student could defend an option OTHER
#     than the keyed one as also correct.
#   * DUPLICATE CONCEPT (item 1) — two questions test the same idea in different
#     words/options (the SEMANTIC dedup CP27's local, options-identical check
#     cannot catch).
# It NEVER rewrites (a rewrite would re-open grounding, which this pass is not
# scored for); failed questions are dropped and refilled through the normal
# grounding gate. FAIL-OPEN throughout: any error, unparseable reply, or omitted
# verdict KEEPS the question — enabling critique can never delete a good question
# on a bad reply. This is the deliberate inverse of the grounding gate's
# fail-closed stance.

_CRITIQUE_PROMPT = """You are reviewing exam questions written from ONE section of a textbook.

SOURCE SECTION:
<<<SOURCE
{text}
SOURCE>>>

QUESTIONS (JSON, each with an index "i"):
{questions}

Judge EACH question against the SOURCE and against the OTHER questions:
- AMBIGUITY: for a multiple-choice question, could a well-prepared student
  reasonably defend an option OTHER than the marked answer as also correct, given
  the SOURCE? If yes, it fails.
- DUPLICATE: does this question test essentially the SAME fact or concept as an
  EARLIER question in the list (even if worded differently)? If yes, mark the
  LATER one as failing.
- OFF-TOPIC: is the question not actually answerable from the SOURCE? If yes, it
  fails.

Return ONLY a JSON array, one object per question, no markdown and no
explanation:
[{{"i": 0, "ok": true}}, {{"i": 1, "ok": false, "reason": "ambiguity"}}]
When unsure, PASS the question (ok: true).
"""


def _parse_critique(raw: str | None, n: int) -> list[bool]:
    """Parse the reply into n keep/drop verdicts (True = keep). FAIL-OPEN: any
    index not EXPLICITLY marked ok:false stays kept, and an unparseable reply
    keeps everything."""
    keep = [True] * n
    text = re.sub(r"```(?:json)?\s*", "", raw or "").strip().rstrip("`").strip()
    data: Any = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                data = None
    if not isinstance(data, list):
        return keep
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and item.get("ok") is False:   # only explicit fail drops
            keep[i] = False
    return keep


def _critique_payload(questions: list[dict[str, Any]]) -> str:
    """The compact JSON the critique reads — index, stem, options, answer only."""
    rows: list[dict[str, Any]] = []
    for i, q in enumerate(questions):
        row: dict[str, Any] = {"i": i, "q": q["question_text"]}
        if q["is_open_ended"]:
            row["ans"] = q["correct_answers"][0]
        else:
            row["opts"] = q["options"]
            row["ans"] = q["correct_answer"]
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False)


def critique_theme_questions(
    questions: list[dict[str, Any]], theme_text: str, *, user_id: int | None = None,
) -> list[bool]:
    """One critique call for a whole theme → a keep/drop flag per question.
    FAIL-OPEN: on ANY exception the flags keep every question (logged, not
    raised), so a misbehaving critique never costs a good question."""
    if not questions:
        return []
    prompt = _CRITIQUE_PROMPT.format(
        text=compress_theme_text(theme_text), questions=_critique_payload(questions),
    )
    try:
        raw, _finish = gemini_rest.call_generation(
            prompt, kind="book_critique", user_id=user_id, call_site="book.critique",
        )
        require_usable_finish(_finish)
    except Exception as exc:
        logger.warning("book_critique_call_failed", error=str(exc))
        return [True] * len(questions)
    return _parse_critique(raw, len(questions))


# ── Media-dependent question detector (CP14) ──────────────────────────────────
#
# Rule 4 of the prompt already instructs the model to skip anything it cannot
# see; this is the enforcement backstop for when it doesn't listen. Only the
# TEXT was extracted, so a question that needs a figure/table/recording is
# unanswerable and must be dropped.
#
# MEASURED against the real 5-theme corpus: short stems are a trap. "фото"
# matches inside "фотосинтез" (photosynthesis — appears 5 times in the real
# book) and "карт" matches inside "картофель" (potato). Every pattern below
# was checked against that corpus and produces ZERO false positives there;
# "карта" (map) is matched as explicit inflected forms rather than a bare
# stem, to also dodge "карточка" (card), which a bounded-length stem would not.
#
# The Uzbek patterns are reasoned, not measured this way — there is no Uzbek
# book in the local corpus yet. "rasm" excludes "rasmiy" ("official") via a
# negative lookahead as the one collision known in advance; a real Uzbek
# corpus should re-run this same false-positive sweep before the assumption is
# trusted the way the Russian side now is.

_KARTA_FORMS = r"\b(?:карта|карты|карте|карту|картой|картою|картам|картами|картах)\b"

_MEDIA_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"рис\.",                 # "(рис. 3)" figure citation — NOT "рис" (rice)
    r"\bрисун\w*",             # рисунок/рисунка/... — NOT "рисовать" (to draw)
    r"\bфотограф\w*",          # фотография — NOT "фотосинтез" (photosynthesis)
    r"\bиллюстрац\w*",
    r"\bдиаграмм\w*",
    r"\bсхем\w*",
    r"\bтаблиц\w*",
    r"\bграфик\w*",
    _KARTA_FORMS,              # inflected "карта" only — NOT "картофель"/"карточка"
    r"\bвидео\w*",
    r"\bаудио\w*",             # NOT "аудитория" (differs at the 5th letter)
    r"\bизображени\w*",
    r"\bпрослуша\w*",
    r"\brasm(?!iy)\w*",        # rasm/rasmlar/rasmda — NOT "rasmiy" (official)
    r"\bsurat\w*",
    r"\bjadval\w*",
    r"\bchizma\w*",
    r"\bxarita\w*",
    r"\btingla\w*",
)]


def references_unavailable_media(
    question_text: str, options: dict[str, str] | None = None,
) -> bool:
    """True if the stem (or an option) points at something only a figure,
    table, audio, or video would show — unanswerable from extracted text."""
    parts = [question_text or ""]
    if options:
        parts.extend(options.values())
    blob = " ".join(parts)
    return any(pattern.search(blob) for pattern in _MEDIA_PATTERNS)


def _question_is_valid(
    question: dict[str, Any], theme_text: str, book_kind: str | None = None,
) -> bool:
    """The full CP14 gate for one already-parsed question.

    CP31a/b: for a WORKBOOK or VOCAB book the distractor-GROUNDING check is
    skipped. A grammar exercise's wrong forms and a vocab list's other-word
    options are DELIBERATELY not the topically-grounded distractors the check
    expects, so requiring them to appear in the text would reject every valid
    question. The ANSWER-grounding invariant is UNCHANGED — the correct
    answer is still a word taken from the source — and distractors_well_formed
    (no blank/repeat) still applies, so the relaxation is narrow and targeted.
    """
    # CP24: a question tied to a PROVIDED figure (img_index set) is grounded in
    # that image, which the text-only CP14 checks cannot verify — the media IS
    # available and the answer/distractors may be visual, not in the prose. Its
    # structural validity (>=2 options + answer among them, or a written answer)
    # was already enforced at parse time, so it is accepted on the model's
    # reading of the figure it was shown. Text questions are unaffected.
    # Item 1: checked BEFORE the image shortcut below. A figure question is
    # exempt from TEXT grounding because its answer may be visual — but its
    # LENGTH is not visual, and a paragraph is just as ungradeable with a
    # picture attached as without one.
    if question["is_open_ended"] and not answer_is_short(
        question["correct_answers"][0]
    ):
        return False
    if question.get("img_index") is not None:
        return True
    if references_unavailable_media(question["question_text"], question.get("options")):
        return False
    if question["is_open_ended"]:
        answer = question["correct_answers"][0]
        return is_answer_grounded(answer, theme_text)
    answer = question["options"][question["correct_answer"]]
    if not is_answer_grounded(answer, theme_text):
        return False
    # CP27: a structurally malformed option set (repeat/blank/length-outlier) is
    # a defect grounding can't see. Rides the same gate so it regenerates.
    if not distractors_well_formed(question["options"], question["correct_answer"]):
        return False
    if book_kind in ("workbook", "vocab"):
        # A workbook's wrong forms and a vocab list's other-word distractors are
        # not the topically-grounded distractors distractors_pass expects; the
        # ANSWER-grounding above is the invariant that matters for these kinds.
        return True
    return distractors_pass(question["options"], question["correct_answer"], theme_text)


def question_passes_grounding(question: dict[str, Any], theme_text: str) -> bool:
    """Public CP14 gate, for callers that re-check an ALREADY-parsed question.

    OPT6's pool serves questions generated in an earlier request; before one is
    handed to a teacher it must clear the exact same grounding bar a freshly
    generated question does — the pool never gets a discount on grounding just
    because the question is cached. This is that same predicate, exposed under a
    public name so the pool does not reach into a private one. Pure and cheap
    (no Gemini call), but a batch of these still runs off the event loop — see
    _run_generation's asyncio.to_thread dispatch.
    """
    return _question_is_valid(question, theme_text)


def _safe_is_valid(
    question: dict[str, Any], theme_text: str, book_kind: str | None = None,
) -> tuple[bool, str | None]:
    """_question_is_valid must never crash a theme's generation (CP15): a bug
    in a predicate is exactly the kind of failure that should surface as
    GenerationResult.errored, the same as a Gemini call raising — not
    propagate uncaught and take the whole theme down with it. Parsed dicts
    are supposed to always have the shape this expects (CP13's defensive
    parsing guarantees it), so this is a backstop, not an expected path.
    """
    try:
        return _question_is_valid(question, theme_text, book_kind), None
    except Exception as e:
        logger.warning("book_generation_validity_check_failed", error=str(e))
        return False, str(e)


# ── Result shape (CP15) ───────────────────────────────────────────────────────
#
# generate_for_theme used to return a bare list[dict], indistinguishable from
# a theme that legitimately supported zero questions. It has zero production
# callers today (Phase 3 has no UI yet), so this breaks nothing real — but the
# FIRST real caller needs to tell a teacher "this theme errored, try again"
# apart from "this section didn't have enough gradable content", which a bare
# list can never carry. Mirrors book_analyzer.BookAnalysis's existing shape:
# payload field first, provenance fields after.
#
# errored is True only when a Gemini call or a validity check actually raised
# — NOT when a regeneration ran cleanly but its replacement was still
# ungrounded. That second case is the drop policy working as designed
# (requested > len(questions) with errored=False), and collapsing it into
# `errored` would blur exactly the distinction CP15 exists to add back.

@dataclass
class GenerationResult:
    questions: list[dict[str, Any]]
    requested: int
    errored: bool
    error: str | None = None
    # How many questions the model RETURNED that are not in `questions`.
    #
    # A shortfall has two causes that look identical from the outside and want
    # opposite responses from a teacher: the section genuinely has less to ask
    # about, or the section is fine and the gate rejected what came back. Only
    # the second produces drops. MEASURED on the real mojibake book: 116
    # returned across four calls, 3 kept — and the teacher was told the section
    # was thin, so they went and inspected a book that was not the problem.
    #
    # DEFINED AS RETURNED MINUS KEPT, subtracted once at the end — never a sum
    # of per-phase rejections. A question that fails the gate and whose batched
    # replacement then succeeds must count as one returned-and-dropped plus one
    # returned-and-kept, not as a loss against a theme that delivered in full.
    #
    # Provenance about an ATTEMPT, not a property of a question: it never
    # touches the question dicts, so nothing reaches the OPT6 pool or a
    # persisted row.
    dropped: int = 0


# ── Prompt ───────────────────────────────────────────────────────────────────
#
# AUDIT item 7 (prefix caching). Gemini's implicit cache matches the LONGEST
# COMMON PREFIX between calls. _ask_in_chunks repeats theme_title, theme_text,
# option_labels, difficulty, question_language and book_kind UNCHANGED across
# a theme's regen/backfill calls — only n_questions/n_mc/n_written differ. The
# count instruction therefore sits at the very END, after every other block,
# and rule 1 below is worded WITHOUT embedding the count — so two calls for
# the same theme that only ask for a different number of questions share the
# entire source-text + rules prefix instead of diverging a few hundred
# characters in. Measured (item 1's telemetry): the theme text alone already
# gives ~86% cache hits since it dominates prompt length; this extends the
# cached span through the rest of the static instructions too.

_PROMPT_TEMPLATE = """You are writing exam questions from ONE section of a textbook.

SOURCE SECTION — "{title}":
<<<SOURCE
{text}
SOURCE>>>

Return ONLY a valid JSON array. No markdown, no code fences, no explanation.

Format:
[{{"q": "question text", "opts": {{{label_example}}}, "ans": "{first_label}", "written": false}}]

For an open-ended question use "opts": {{}} and put the expected answer in "ans", with "written": true.

RULES — the first is the most important:
1. GROUNDING. Every correct answer MUST be a fact stated explicitly in the
   SOURCE above. Never invent information, never use outside knowledge, never
   infer a fact the text does not state. If the section does not support the
   requested number of good questions, return fewer — a short honest list is
   right, a padded one is not.
2. DISTRACTORS. Wrong options must be plausible and CONTEXTUALLY CLOSE: use
   real, related entities that actually appear in this section (other people,
   dates, terms, organs, processes named in the text). Never invent names, and
   never use obviously absurd options — a distractor should be wrong to someone
   who studied, not to everyone.
3. USE THE BOOK'S OWN EXERCISES when the section already contains complete
   practice questions that stand on their own from the text alone. Prefer them
   over writing new ones.
4. SKIP anything that depends on material you cannot see: audio, video, a
   figure, a photograph, a map, a table's layout, or "look at the picture".
   Only the TEXT was extracted, so such a question cannot be answered.
5. OPTION LABELS. Use exactly these labels, in this order: {labels}.
   Use them verbatim — do not translate, transliterate or re-order them.
6. LANGUAGE. Write every question and every option in the SAME LANGUAGE as the
   SOURCE text above. Do not translate the material into another language.
7. Exactly one option per multiple-choice question is correct, and "ans" must
   be one of the labels listed in rule 5.
8. VARY THE QUESTION TYPES across the set — do not make them all single-fact
   recall. Draw on a mix as the SOURCE allows: definition, classification,
   comparison, cause-and-effect, process or sequence, and applying a stated
   rule or definition to a new case.
{sample_block}{difficulty_block}{image_block}{rubric_block}{language_block}{kind_block}
Write exactly {n_questions} question(s): {n_mc} multiple-choice and {n_written} open-ended (written-answer).{avoid_block}"""

# CP32 / AUDIT C-2: appended ONLY to a follow-up call (regen, backfill, refill,
# or a later chunk of one ask). Empty on a theme's FIRST call, so that prompt is
# byte-identical to before. Deliberately placed AFTER the count instruction:
# A1 moved the count last so two calls for the same theme differ only in their
# tail, and this block varies per call for exactly the same reason.
_AVOID_BLOCK = """

ALREADY WRITTEN for this section — do NOT repeat any of these, and do not ask
the same thing in different words. Cover DIFFERENT parts of the SOURCE:
{stems}"""

# CP24: appended ONLY when the theme has captured figures AND the teacher opted
# in. Empty otherwise, so a text-only run's prompt is byte-identical to pre-CP24.
_IMAGE_BLOCK = """
IMAGES. You are ALSO given {n_images} image(s) from this section, numbered 1 to
{n_images} in the order attached. You MAY write up to {n_images} IMAGE-BASED
question(s). Each such question references EXACTLY ONE image by its number in an
"img" field (1-based), e.g.
  {{"q": "Rasmda ko'rsatilgan ... qaysi?", "opts": {{{label_example}}}, "ans": "{first_label}", "img": 1}}
For a question with an "img" field, rule 4 does NOT apply to THAT image — you
CAN and SHOULD ask about what the image itself shows. The correct answer must
still be grounded in the SOURCE text or in the image's own visible content.
NEVER use an "img" number outside 1..{n_images}, and never invent an image you
were not given. A question with no "img" field is a normal text question and
rule 4 still applies to it.
"""

_SAMPLE_BLOCK = """
STYLE REFERENCE — the teacher supplied this example ONLY to show the style,
tone, phrasing and length they prefer:
<<<EXAMPLE
{sample}
EXAMPLE>>>
Match its STYLE. Do NOT reuse its content, its subject matter or its answer —
the questions must still come only from the SOURCE section above.
"""

# CP20: a skew, not a 100% single tier — an all-hard test is not what a
# teacher asking for "a harder version" actually wants. "medium" (the
# default) adds no block at all, so it is byte-identical to pre-CP20 output.
# (Numbered rule 9 since CP28 added the always-on type-mix rule 8.)
_DIFFICULTY_BLOCKS = {
    "easy": """
9. DIFFICULTY. The teacher asked for an EASIER test: make MOST questions
   straightforward recall of a single fact stated directly in the SOURCE,
   with at most one or two that require connecting two facts. Avoid
   multi-step inference as the norm.""",
    "hard": """
9. DIFFICULTY. The teacher asked for a HARDER test: make MOST questions
   require connecting two or more facts from the SOURCE, or applying a
   stated rule or definition to a new instance, with at most one or two
   straightforward recall questions. Avoid trivial single-fact recall as
   the norm.""",
}

# CP28 + Item 1: the open-ended block. Appended ONLY when the request asks for
# at least one written question, so an all-MC prompt is byte-identical to
# pre-CP28 (same discipline as the CP24 image block).
#
# Item 1 added the SHORT-ANSWER requirement. A written answer is graded by
# checker.is_correct, an EXACT match against what the student wrote by hand, so
# a multi-sentence reference answer is not a harder question — it is an
# ungradeable one. MEASURED over 8 real calls on kimyo 7 (uz) and biologiya 6
# (ru): without this block the median answer ran 267 chars / 35 words in Uzbek
# and 134 / 15 in Russian, and NOT ONE answer in either language came in at 3
# words or fewer. With it, the Uzbek median is 14 chars / 2 words (90% within 3
# words) and the Russian 10 chars / 1 word (100%).
#
# It also tells the model to convert rather than stretch: an idea that only
# answers in a sentence should become a multiple-choice question, which is what
# keeps this from simply losing coverage of those parts of the source.
#
# key_points is unchanged — stored internally for a FUTURE answer-checking
# upgrade, not shown to the teacher and not used by today's grading.
_RUBRIC_BLOCK = """
OPEN-ENDED ANSWER FORMAT (applies to every question with "written": true).
The "ans" value is the answer key a teacher grades against by EXACT comparison
with what the student wrote by hand, so it MUST be short and unambiguous:
a single word, a short phrase of AT MOST 3 words, a date, a number, a proper
name, or one specific term. NEVER a sentence, never an explanation, never a
definition spelled out, never more than 3 words.
Ask an open-ended question ONLY when the SOURCE supports an answer that is
naturally that short. If a point can only be answered in a sentence, write it
as a MULTIPLE-CHOICE question instead - do not stretch "ans" to fit.
ALSO include a "key_points" field for each open-ended question: a JSON array of
2-4 short phrases naming the essential points a correct answer covers, each
grounded in the SOURCE. Example:
  {{"q": "...", "opts": {{}}, "ans": "1869", "written": true, "key_points": ["point one", "point two"]}}
"""

# CP30: language normalization v1. Appended ONLY when the teacher picks a question
# STEM language different from the source; empty otherwise → byte-identical to
# pre-CP30 (rule 6 alone). The ANSWER (and options) stay in the SOURCE language on
# purpose: grounding scores the answer against the source text, so a
# source-language answer keeps the CP14 gate intact while the stem floats freely.
_LANGUAGE_BLOCK = """
LANGUAGE OF THE QUESTION STEM. Overriding rule 6 for the STEM only: write each
question's stem (the wording before the options) in {lang_name}. Rule 6 still
holds for the OPTIONS and the correct ANSWER — keep those in the SOURCE language,
untranslated, exactly as the text uses them. Translate ONLY the stem.
"""

# Human-readable names for the prompt. Any code the caller passes that is not
# here (or matches the source) yields no override block — a safe no-op.
_LANG_NAMES = {"uz": "Uzbek", "ru": "Russian", "en": "English"}

# CP31a: appended ONLY for a WORKBOOK book (BookKind.WORKBOOK); empty for a
# narrative textbook → byte-identical to pre-CP31 (same discipline as every other
# trailing block). Turns "write questions ABOUT the text" into "write the
# exercises the workbook is made of", while keeping the grounding invariant: the
# answer is still a word taken from the SOURCE, so the CP14 gate is unaffected.
_WORKBOOK_BLOCK = """
WORKBOOK MODE. This SOURCE is from a grammar/exercise workbook, so write
EXERCISE-STYLE questions, NOT questions about the text:
- FILL-IN-THE-BLANK: take a sentence from the SOURCE, remove one key word or
  grammatical form, and make the removed item the correct answer; the options
  are plausible alternative words/forms.
- CHOOSE-THE-CORRECT-FORM: present a sentence with a gap and offer grammatical
  forms as the options (verb tenses, prepositions, articles, etc.).
Rule 1 (GROUNDING) still holds: the correct answer must be exactly the word or
form the SOURCE sentence actually uses.
"""

# CP31b: appended only for a VOCAB book. The SOURCE is a word/translation list,
# so both sides of every pair are IN the source text — which is exactly why the
# answer stays grounded whichever translation direction the model picks.
_VOCAB_BLOCK = """
VOCABULARY MODE. This SOURCE is a vocabulary / word list — pairs of a word and
its translation or meaning. Write VOCABULARY questions:
- TRANSLATION: give a word from the SOURCE and ask for its translation/meaning;
  the correct answer is that word's paired translation FROM THE SOURCE, and the
  options are OTHER translations that appear in the SOURCE list.
- WORD-FOR-MEANING: give a meaning/translation and ask which SOURCE word it is.
Use ONLY words and translations that actually appear in the SOURCE. Rule 1
(GROUNDING) holds: every correct answer is a word taken directly from the SOURCE.
"""


def build_prompt(
    *, theme_title: str, theme_text: str, n_questions: int, n_written: int,
    option_labels: list[str], sample_question: str | None, language: str | None,
    difficulty: str | None = None, n_images: int = 0,
    question_language: str | None = None, book_kind: str | None = None,
    accepted_questions: list[str] | None = None,
) -> str:
    """Assemble the generation prompt for ONE theme.

    `language` is deliberately NOT used to name a target language: the detector
    is known to be unreliable on thin samples (Checkpoint 3), so rule 6 pins
    the output to the source text itself. The label is kept in the signature
    because callers have it and it is logged.

    `difficulty` is "easy" / "medium" / "hard" (CP20). "medium" and None both
    mean no skew instruction at all — the pre-CP20 default, unchanged.

    `n_images` (CP24) is how many theme figures are attached to the call. 0
    means no image block at all — byte-identical to the pre-CP24 prompt.

    `question_language` (CP30) names the language for the question STEM only. None
    (or the source language, or an unrecognized code) adds no block — byte-
    identical to pre-CP30. A recognized code writes stems in that language while
    keeping options + the answer in the SOURCE language, so grounding (which
    scores the answer) is unaffected.

    `accepted_questions` (AUDIT C-2) are the stems this theme has ALREADY kept,
    so a regen/backfill call stops re-deriving them. Empty or None adds no block
    — a theme's first call is byte-identical to pre-C-2.
    """
    labels = [str(lab) for lab in option_labels if str(lab).strip()] or ["A", "B", "C", "D"]
    n_written = max(0, min(n_written, n_questions))
    n_mc = n_questions - n_written

    label_example = ", ".join(f'"{lab}": "option text"' for lab in labels)
    sample_block = (
        _SAMPLE_BLOCK.format(sample=sample_question.strip())
        if sample_question and sample_question.strip() else ""
    )
    difficulty_block = _DIFFICULTY_BLOCKS.get(difficulty or "", "")
    image_block = (
        _IMAGE_BLOCK.format(
            n_images=n_images, label_example=label_example, first_label=labels[0],
        )
        if n_images > 0 else ""
    )
    # CP28: rubric block ONLY when at least one written question is asked for, so
    # an all-MC prompt stays byte-identical to pre-CP28. .format() collapses the
    # doubled braces in the JSON example to single ones (same as the other blocks).
    rubric_block = _RUBRIC_BLOCK.format() if n_written > 0 else ""
    # CP30: stem-language override ONLY for a recognized, non-source language;
    # None / unknown → empty → byte-identical to pre-CP30 (rule 6 alone).
    lang_name = _LANG_NAMES.get((question_language or "").lower())
    language_block = _LANGUAGE_BLOCK.format(lang_name=lang_name) if lang_name else ""
    # CP31a/b: an exercise (workbook) or vocabulary (vocab) prompt block; empty
    # for a narrative book → byte-identical to pre-CP31.
    if book_kind == "workbook":
        kind_block = _WORKBOOK_BLOCK
    elif book_kind == "vocab":
        kind_block = _VOCAB_BLOCK
    else:
        kind_block = ""
    # C-2: the stems already kept for this theme. Empty → no block at all.
    stems = [
        " ".join(str(q).split())[:AVOID_STEM_CHARS]
        for q in (accepted_questions or [])
        if str(q).strip()
    ]
    avoid_block = (
        _AVOID_BLOCK.format(stems="\n".join(f"- {s}" for s in stems[-AVOID_MAX_STEMS:]))
        if stems else ""
    )
    # OPT4: compress the SOURCE text for the prompt only. Grounding still uses
    # the full theme_text (in _question_is_valid), so nothing factual is at risk.
    return _PROMPT_TEMPLATE.format(
        title=theme_title, text=compress_theme_text(theme_text),
        n_questions=n_questions, n_mc=n_mc, n_written=n_written,
        labels=", ".join(labels), first_label=labels[0],
        label_example=label_example, sample_block=sample_block,
        difficulty_block=difficulty_block, image_block=image_block,
        rubric_block=rubric_block, language_block=language_block,
        kind_block=kind_block, avoid_block=avoid_block,
    )


# ── Parsing the model's reply ────────────────────────────────────────────────

def _salvage_objects(text: str) -> list[dict]:
    """Pull complete {...} objects out of a truncated array.

    max_output_tokens can cut a reply mid-array. Losing four good questions
    because the fifth was clipped would be a silent, expensive waste, so
    complete objects before the break are recovered.
    """
    out: list[dict] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    out.append(json.loads(text[start:i + 1]))
                except (json.JSONDecodeError, ValueError):
                    pass
                start = -1
    return out


# CP28: at most this many rubric key points are kept — a rubric is a short
# checklist, not an essay; a runaway list is defensively truncated.
_MAX_KEY_POINTS = 5


def _parse_key_points(raw: Any) -> list[str] | None:
    """Defensively coerce a model-supplied "key_points" into a clean list of
    short strings, or None when absent/unusable. Never raises: a malformed
    rubric must not cost a valid question."""
    if not isinstance(raw, list):
        return None
    points = [str(p).strip() for p in raw if p is not None and str(p).strip()]
    return points[:_MAX_KEY_POINTS] or None


def parse_generation_response(raw: str | None, *, n_images: int = 0) -> list[dict[str, Any]]:
    """Defensive parse into the question dicts the existing pipeline expects.

    Same shape variant_generator/pdf_generator already consume, so nothing
    downstream needs to change: question_text / options / correct_answer /
    correct_answers / is_open_ended.

    CP24: a valid 1-based "img" field (1..n_images) is carried as `img_index` on
    the question so the caller can resolve it to a real theme figure. An out-of-
    range or absent img is simply ignored (a normal text question).

    Anything unusable is DROPPED rather than repaired — a question whose answer
    is not among its own options cannot be graded, and guessing which option was
    meant would silently invent an answer key.
    """
    text = re.sub(r"```(?:json)?\s*", "", raw or "").strip().rstrip("`").strip()
    if not text:
        return []

    data: Any = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                data = None
        if data is None:
            data = _salvage_objects(text)

    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question_text = str(item.get("q") or "").strip()
        if not question_text:
            continue

        raw_opts = item.get("opts") or {}
        options: dict[str, str] = {}
        if isinstance(raw_opts, dict):
            for label, value in raw_opts.items():
                label_s = str(label).strip()
                value_s = str(value).strip() if value is not None else ""
                if label_s and value_s:
                    options[label_s] = value_s

        answer = str(item.get("ans") or "").strip()
        written = bool(item.get("written"))
        if not written and not options:
            # AUDIT B-23. This used to be `written = ... or not options`, which
            # silently RE-CLASSIFIED a multiple-choice question whose options
            # failed to parse (opts returned as a list, or with empty values) as
            # a WRITTEN one. The teacher asked for N multiple-choice and got
            # typed-answer questions, the theme's written/MC accounting was
            # wrong, and nothing was logged — exactly the class this parser's
            # docstring claims to prevent ("Anything unusable is DROPPED rather
            # than repaired").
            #
            # The distinction that matters: options OFFERED but unusable is a
            # parse failure and must be dropped; NO options offered at all is a
            # genuine written question (an older prompt omits the flag).
            if raw_opts:
                logger.warning(
                    "generation_options_unusable",
                    question=question_text[:60],
                    opts_type=type(raw_opts).__name__,
                )
                continue
            written = True

        # CP24: a valid 1-based image reference, else None (normal text question).
        img_index: int | None = None
        if n_images > 0:
            try:
                idx = int(item.get("img"))
                if 1 <= idx <= n_images:
                    img_index = idx
            except (TypeError, ValueError):
                img_index = None

        if written:
            if not answer:
                continue
            out.append({
                "question_text": question_text,
                "options": {},
                "correct_answer": None,
                "correct_answers": [answer],
                "is_open_ended": True,
                "img_index": img_index,
                # CP28: expected key points for a future answer-checker. None when
                # the model omitted them (older prompt / MC-only run), so the
                # field is always safe to read but never fabricated.
                "rubric": _parse_key_points(item.get("key_points")),
            })
            continue

        # Multiple choice needs at least two options and an answer that names
        # one of them, or it is not gradeable.
        if len(options) < 2 or answer not in options:
            continue
        out.append({
            "question_text": question_text,
            "options": options,
            "correct_answer": answer,
            "correct_answers": [answer],
            "is_open_ended": False,
            "img_index": img_index,
        })
    return out


# ── The call ─────────────────────────────────────────────────────────────────

def _load_gen_images(theme_images: list[dict] | None) -> list[tuple[bytes, dict]]:
    """CP24: load the ≤384px generation blob for each theme figure, keeping it
    paired with its row so a returned img index maps back to the real image.
    Silently drops any figure whose file can't be read, so the surviving list
    is contiguous and its 1-based order matches what the prompt describes."""
    pairs: list[tuple[bytes, dict]] = []
    for ti in theme_images or []:
        path = ti.get("gen_image_path") or ti.get("image_path")
        if not path:
            continue
        try:
            with open(path, "rb") as f:
                pairs.append((f.read(), ti))
        except Exception as e:
            logger.warning("gen_image_load_failed", path=path, error=str(e))
    return pairs


def _call_and_parse(
    *, theme_title: str, theme_text: str, n_questions: int, n_written: int,
    option_labels: list[str], sample_question: str | None,
    language: str | None, user_id: int | None, difficulty: str | None = None,
    theme_images: list[dict] | None = None, question_language: str | None = None,
    book_kind: str | None = None, accepted_questions: list[str] | None = None,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """One Gemini call for one theme, parsed. No grounding check — used for
    both the initial request and the CP14 regeneration attempt.

    Returns (questions, errored, error) rather than raising: one theme going
    wrong must not abort a multi-theme request. CP15: errored/error carry
    forward to GenerationResult instead of being swallowed here — a caller
    that only sees an empty list can't tell "nothing to ask" from "the call
    blew up", which is exactly the gap CP15 closes.

    CP24: `theme_images` (opt-in) attaches the theme's figures multimodally so
    the model can write image-based questions. A returned img index is resolved
    here to the real figure's HIGH-DPI image_path + has_image=True, so the rest
    of the pipeline (persist, PDF embed) sees the same shape the upload flow
    produces.
    """
    pairs = _load_gen_images(theme_images)
    n_images = len(pairs)
    prompt = build_prompt(
        theme_title=theme_title, theme_text=theme_text,
        n_questions=n_questions, n_written=n_written,
        option_labels=option_labels, sample_question=sample_question,
        language=language, difficulty=difficulty, n_images=n_images,
        question_language=question_language, book_kind=book_kind,
        accepted_questions=accepted_questions,
    )
    try:
        raw, finish_reason = gemini_rest.call_generation(
            prompt, kind="book_generate", user_id=user_id, call_site="book.generate",
            timeout=GENERATION_TIMEOUT, max_output_tokens=MAX_OUTPUT_TOKENS,
            images=[blob for blob, _ in pairs] or None,
        )
        require_usable_finish(finish_reason)
    except Exception as e:
        logger.warning("book_generation_failed", theme=theme_title[:40], error=str(e))
        return [], True, str(e)

    questions = parse_generation_response(raw, n_images=n_images)
    # CP24: resolve each image-tied question to its real high-DPI figure.
    # img_index stays on the dict so the CP14 gate (generate_for_theme) can see
    # it and skip text-grounding for image questions; downstream ignores it.
    for q in questions:
        idx = q.get("img_index")
        if idx is not None and 1 <= idx <= n_images:
            q["has_image"] = True
            q["image_path"] = pairs[idx - 1][1].get("image_path")
    if finish_reason == 2:
        logger.warning(
            "book_generation_truncated",
            theme=theme_title[:40], salvaged=len(questions), asked=n_questions,
        )
    logger.info(
        "book_generation_theme_done",
        theme=theme_title[:40], asked=n_questions, got=len(questions),
    )
    return questions, False, None


def _ask_in_chunks(
    *, theme_title: str, theme_text: str, n_questions: int, n_written: int,
    option_labels: list[str], sample_question: str | None, language: str | None,
    user_id: int | None, difficulty: str | None = None,
    theme_images: list[dict] | None = None, question_language: str | None = None,
    book_kind: str | None = None, accepted_questions: list[str] | None = None,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Ask for `n_questions`, split so NO single call exceeds
    MAX_QUESTIONS_PER_CALL (AUDIT B-65).

    Used by every site that asks for questions — the initial ask, the CP14
    regeneration and the CP26 backfill — because a large shortfall recreates
    exactly the truncation the initial chunking prevents.

    n_questions <= MAX_QUESTIONS_PER_CALL takes exactly one call, so the common
    case is byte-identical to before.

    Stops at the first call that RAISES: a hard failure must not be retried as
    if it were mere under-delivery, the same rule the regen and backfill follow.

    C-2: `accepted_questions` is what the THEME has already kept (empty on the
    initial ask), and each chunk after the first also carries what the earlier
    chunks of this same ask just returned — chunk 2 blindly re-deriving chunk
    1's questions is the same defect as a backfill round doing it.
    """
    collected: list[dict[str, Any]] = []
    remaining, remaining_written = max(0, n_questions), max(0, n_written)
    first = True
    while remaining > 0:
        chunk = min(remaining, MAX_QUESTIONS_PER_CALL)
        # Written questions go to the EARLIEST chunks rather than being
        # repeated in full on each one, mirroring distribute_questions' rule.
        chunk_written = min(remaining_written, chunk)
        part, part_errored, part_error = _call_and_parse(
            theme_title=theme_title, theme_text=theme_text,
            n_questions=chunk, n_written=chunk_written,
            option_labels=option_labels, sample_question=sample_question,
            language=language, user_id=user_id, difficulty=difficulty,
            # CP24 figures are a bounded per-theme resource (<= 4). Re-sending
            # them on every chunk would multiply image tokens and invite
            # duplicate figure questions, so only the first chunk carries them.
            theme_images=theme_images if first else None,
            question_language=question_language, book_kind=book_kind,
            accepted_questions=(
                list(accepted_questions or [])
                + [q["question_text"] for q in collected]
            ),
        )
        collected.extend(part)
        if part_errored:
            return collected, True, part_error
        remaining -= chunk
        remaining_written -= chunk_written
        first = False
    return collected, False, None


def generate_for_theme(
    *, theme_title: str, theme_text: str, n_questions: int, n_written: int,
    option_labels: list[str], sample_question: str | None,
    language: str | None, user_id: int | None, difficulty: str | None = None,
    theme_images: list[dict] | None = None, question_language: str | None = None,
    book_kind: str | None = None,
) -> GenerationResult:
    """Generate GROUNDED questions for ONE theme. Blocking — call via
    asyncio.to_thread.

    CP14 enforcement, applied here so no caller can forget it: every returned
    question passes references_unavailable_media + is_answer_grounded (+
    distractors_pass for multiple choice). A question that fails gets ONE
    regeneration attempt — batched into a single follow-up call for the whole
    theme, not one call per failing question, matching this module's
    call-economy policy — and is dropped if the replacement also fails.

    CP15: returns GenerationResult, never raises. `errored` is True only when
    a Gemini call or a validity check itself raised — not when a
    regeneration ran cleanly but its replacement was still ungrounded (that's
    the drop policy working as designed, not an error).

    CP20: `difficulty` ("easy"/"medium"/"hard") is a prompt-wording skew, not
    a per-question tagged field — passed through unchanged to the
    regeneration call too, so a regenerated replacement matches the same
    skew the teacher asked for.

    CP26: after the CP14 regen, if the theme is STILL short of n_questions
    (the model under-delivered, or dedup/format dropped some), up to
    BACKFILL_MAX_ROUNDS more batched calls chase the shortfall, stopping early
    on an unproductive round or a raised call. This is what stops a teacher who
    asked for N from silently receiving fewer.

    CP27: near-duplicate questions are dropped (per theme here; once more across
    themes at aggregation), and the CP14 gate now also rejects malformed option
    sets (see distractors_well_formed) so a broken question regenerates instead
    of shipping.

    CP29b: when settings.BOOK_CRITIQUE_ENABLED (DEFAULT OFF), a final critique
    call flags ambiguous / concept-duplicate questions, which are dropped and
    refilled once. Off → this function is byte-identical to CP29a.
    """
    if n_questions <= 0 or not (theme_text or "").strip():
        # Nothing to ask, or nothing to ground against — either way, asking
        # would spend a request and invite invention. Not an error.
        return GenerationResult(questions=[], requested=n_questions, errored=False)

    questions, errored, error = _ask_in_chunks(
        theme_title=theme_title, theme_text=theme_text,
        n_questions=n_questions, n_written=n_written,
        option_labels=option_labels, sample_question=sample_question,
        language=language, user_id=user_id, difficulty=difficulty,
        theme_images=theme_images, question_language=question_language,
        book_kind=book_kind,
    )
    # What the MODEL returned, accumulated across every call this attempt makes.
    # Counting what came back — never counting rejections — is what lets
    # `dropped` be one subtraction at the end instead of a per-phase tally.
    returned = len(questions)
    # A call that RAISED is an infrastructure failure, not under-delivery: it
    # tells us nothing about whether the section can support the questions, so
    # the CP14 regen and the CP26 backfill below both refuse to spin more calls
    # once one has raised (a hard failure must not be retried as if it were
    # merely ungrounded — see test_a_failed_call_returns_no_questions...).
    call_errored = errored

    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for q in questions:
        ok, check_error = _safe_is_valid(q, theme_text, book_kind)
        if check_error is not None:
            errored = True
            error = error or check_error
        (passed if ok else failed).append(q)
    # CP27: collapse near-duplicates the model returned in one batch; the freed
    # slots are chased by the backfill below, so dedup never silently shrinks
    # the delivered count.
    passed = dedupe_questions(passed)

    # CP14: one batched regeneration for the questions that failed the gate.
    if failed and not call_errored:
        logger.info(
            "book_generation_regenerating_ungrounded",
            theme=theme_title[:40], failed=len(failed), passed=len(passed),
        )
        n_failed_written = sum(1 for q in failed if q["is_open_ended"])
        replacements, regen_errored, regen_error = _ask_in_chunks(
            theme_title=theme_title, theme_text=theme_text,
            n_questions=len(failed), n_written=n_failed_written,
            option_labels=option_labels, sample_question=sample_question,
            language=language, user_id=user_id, difficulty=difficulty,
            question_language=question_language, book_kind=book_kind,
            # C-2: only the KEPT stems. The rejected ones are the ground this
            # call must now cover, so listing them would be exactly backwards.
            accepted_questions=[q["question_text"] for q in passed],
        )
        returned += len(replacements)
        if regen_errored:
            errored = True
            error = error or regen_error
            call_errored = True
        for q in replacements:
            ok, check_error = _safe_is_valid(q, theme_text, book_kind)
            if check_error is not None:
                errored = True
                error = error or check_error
            if ok and not _is_near_duplicate(q, passed):
                passed.append(q)
            # else: regenerated once and still failed (or duplicated a kept
            # one) — dropped, per policy. Never guessed at or included anyway.

    # CP26: still short of the target? Chase the remaining shortfall with up to
    # BACKFILL_MAX_ROUNDS more batched calls — this is what fixes the visible
    # bug where a model that simply returns fewer questions than asked (the
    # common case, or a dedup/format drop) left the teacher silently short. Each
    # round asks ONLY for what is missing, keeps only grounded, well-formed,
    # non-duplicate replies, and stops the instant a round adds nothing new: an
    # empty round means the section is out of gradable material and asking again
    # would only spend a request to reinvent the same misses. Backfill rounds
    # are free to the teacher (they never requested the shortfall) — the charge
    # is one unit per ATTEMPT, levied by the handler, not per Gemini call.
    rounds = 0
    while len(passed) < n_questions and rounds < BACKFILL_MAX_ROUNDS and not call_errored:
        rounds += 1
        shortfall = n_questions - len(passed)
        have_written = sum(1 for q in passed if q["is_open_ended"])
        short_written = max(0, min(n_written - have_written, shortfall))
        logger.info(
            "book_generation_backfilling",
            theme=theme_title[:40], round=rounds, shortfall=shortfall,
        )
        more, more_errored, more_error = _ask_in_chunks(
            theme_title=theme_title, theme_text=theme_text,
            n_questions=shortfall, n_written=short_written,
            option_labels=option_labels, sample_question=sample_question,
            language=language, user_id=user_id, difficulty=difficulty,
            question_language=question_language, book_kind=book_kind,
            accepted_questions=[q["question_text"] for q in passed],
        )
        returned += len(more)
        if more_errored:
            errored = True
            error = error or more_error
            break
        added = 0
        for q in more:
            ok, check_error = _safe_is_valid(q, theme_text, book_kind)
            if check_error is not None:
                errored = True
                error = error or check_error
            if ok and not _is_near_duplicate(q, passed):
                passed.append(q)
                added += 1
        if added == 0:
            break

    # CP29b (guard-gated, DEFAULT OFF → byte-identical to CP29a when off): one
    # critique call flags ambiguous / concept-duplicate questions; those are
    # dropped and refilled ONCE (grounded + deduped, not re-critiqued, so at most
    # one critique + one refill call is added per attempt). Skipped when a call
    # already raised — there is nothing trustworthy to critique. Critique itself
    # is fail-open (a bad reply keeps everything); only the refill, a real
    # generation call, can set errored, exactly like a backfill round.
    if settings.BOOK_CRITIQUE_ENABLED and passed and not call_errored:
        keep = critique_theme_questions(passed, theme_text, user_id=user_id)
        kept = [q for q, ok in zip(passed, keep) if ok]
        # Named apart from the `dropped` computed below: that one is the
        # attempt-wide returned-minus-kept figure, this is only what THIS
        # critique pass removed. Both are real numbers and neither should
        # quietly become the other.
        critique_dropped = len(passed) - len(kept)
        if critique_dropped:
            logger.info(
                "book_generation_critique_dropped",
                theme=theme_title[:40], dropped=critique_dropped, kept=len(kept),
            )
            passed = kept
            shortfall = n_questions - len(passed)
            if shortfall > 0:
                have_written = sum(1 for q in passed if q["is_open_ended"])
                short_written = max(0, min(n_written - have_written, shortfall))
                more, more_errored, more_error = _ask_in_chunks(
                    theme_title=theme_title, theme_text=theme_text,
                    n_questions=shortfall, n_written=short_written,
                    option_labels=option_labels, sample_question=sample_question,
                    language=language, user_id=user_id, difficulty=difficulty,
                    question_language=question_language, book_kind=book_kind,
                    accepted_questions=[q["question_text"] for q in passed],
                )
                returned += len(more)
                if more_errored:
                    errored = True
                    error = error or more_error
                for q in more:
                    ok, check_error = _safe_is_valid(q, theme_text, book_kind)
                    if check_error is not None:
                        errored = True
                        error = error or check_error
                    if ok and not _is_near_duplicate(q, passed):
                        passed.append(q)

    # ONE subtraction, at the end: what the model produced this attempt minus
    # what is actually being used. See GenerationResult.dropped for why it is
    # not a running tally of rejections.
    dropped = max(0, returned - len(passed))
    if dropped:
        logger.info(
            "book_generation_questions_dropped",
            theme=theme_title[:40], requested=n_questions,
            returned=returned, kept=len(passed), dropped=dropped,
        )
    return GenerationResult(passed, n_questions, errored, error, dropped=dropped)
