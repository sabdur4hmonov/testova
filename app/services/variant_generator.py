"""
Variant generation engine — group-aware anti-cheating shuffle.

Rules:
  - Reading-comprehension groups (shared context_text) move as ONE unit.
    Their internal question order stays fixed; only option letters are shuffled.
  - Single questions: both question order and option letters are shuffled.
  - No two adjacent variants share the same unit arrangement (where possible).
"""
from __future__ import annotations

import random
import uuid
from typing import Any

from app.services.option_letters import canonical_letter
from app.utils.logging import get_logger

logger = get_logger(__name__)


# ── Pre-export validation ───────────────────────────────────────────────────────

def validate_questions(
    questions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    BUG FIX: validate questions BEFORE variants/PDFs are built.
    Previously nothing checked question integrity, so broken questions
    (blank options, missing text) went straight into the printed exam.

    Rules:
      - empty question_text                      → rejected
      - 0 non-empty options                      → kept, marked is_open_ended
      - exactly 1 non-empty option               → rejected (not gradeable MC)
      - blank option strings among 2-4 filled    → blanks dropped pre-shuffle
      - correct_answer points at a dropped blank → rejected (key unrecoverable)

    Returns (valid_questions, rejected) where each rejected entry is
    {"question_number": int, "reason": str}.
    """
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for q in questions:
        q = {**q}  # don't mutate caller's dicts
        num = q.get("question_number", "?")

        if not str(q.get("question_text") or "").strip():
            rejected.append({"question_number": num, "reason": "empty_text"})
            continue

        opts = q.get("options") or {}
        filled = {
            k: v for k, v in opts.items()
            if v is not None and str(v).strip()
        }

        if len(filled) == 0:
            # Genuinely open-ended — keep, but mark it so the PDF renders
            # a write-in line instead of an empty options block.
            q["options"] = {}
            q["is_open_ended"] = True
            valid.append(q)
            continue

        if len(filled) == 1:
            rejected.append({"question_number": num, "reason": "single_option"})
            continue

        ca = q.get("correct_answer")
        if ca and ca in opts and ca not in filled:
            # The correct option itself was blank — the answer key would
            # point at nothing after blanks are dropped.
            rejected.append({"question_number": num, "reason": "blank_correct_option"})
            continue

        # Drop blank options but PRESERVE the real labels and order — never
        # renumber or fill gaps (letter preservation, KNOWN-OPEN #2). "a,b,d,e"
        # stays "a,b,d,e"; Cyrillic stays Cyrillic. correct_answer already names
        # a real, non-blank label (checked above), so it is left as-is.
        q["options"] = dict(filled)
        q["is_open_ended"] = False
        valid.append(q)

    if rejected:
        logger.warning(
            "questions_rejected",
            count=len(rejected),
            details=rejected,
        )
    return valid, rejected


# ── Option shuffler ─────────────────────────────────────────────────────────────

def _shuffle_options(
    question: dict[str, Any], rng: random.Random
) -> tuple[dict[str, str], str | None]:
    original = question.get("options", {})
    # REAL labels in printed order (any script, any gaps). Shuffling permutes the
    # TEXTS across these fixed labels; the labels themselves never change.
    available = [k for k in original if original.get(k) is not None]

    if len(available) < 2:
        return original, question.get("correct_answer")

    positions = available[:]
    rng.shuffle(positions)

    shuffled: dict[str, str] = {}
    option_map: dict[str, str] = {}  # original → new letter

    for new_letter, orig_letter in zip(available, positions):
        shuffled[new_letter] = original[orig_letter]
        option_map[orig_letter] = new_letter

    orig_correct = question.get("correct_answer")
    new_correct = option_map.get(orig_correct) if orig_correct else None
    return shuffled, new_correct


# ── Unit builder ────────────────────────────────────────────────────────────────

def _build_units(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Group questions into shuffleable units.
    Reading-comprehension questions with the same group_id form one unit.
    All others are individual units.
    """
    units: list[dict[str, Any]] = []
    seen: dict[str, int] = {}  # group_id → unit index

    for q in questions:
        gid = q.get("group_id")
        if gid and gid in seen:
            units[seen[gid]]["questions"].append(q)
        else:
            unit: dict[str, Any] = {
                "group_id": gid,
                "group_context": q.get("group_context"),
                "is_reading_group": bool(gid),
                "questions": [q],
            }
            if gid:
                seen[gid] = len(units)
            units.append(unit)

    return units


# ── Core generator ──────────────────────────────────────────────────────────────

def generate_variants(
    questions: list[dict[str, Any]],
    count: int,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """
    Generate `count` variants from questions.

    Returns list of variant dicts:
    {
        "variant_number": int,
        "question_order": [question_id, ...],
        "option_mapping": {question_id: {orig_letter: new_letter}},
        "answer_key": {"1": "B", "2": "A", ...},
        "questions_data": [...],   # fully resolved, for PDF
    }
    """
    if not questions:
        raise ValueError("Cannot generate variants from empty question list")

    # BUG FIX: never export unvalidated questions. Callers (upload handler)
    # should run validate_questions themselves to report rejects to the
    # teacher; this pass is defense-in-depth for any other entry point.
    questions, _rejected = validate_questions(questions)
    if not questions:
        raise ValueError("No valid questions left after validation")

    units = _build_units(questions)
    base_rng = random.Random(seed)
    seen_orders: set[tuple] = set()
    variants: list[dict[str, Any]] = []

    for variant_num in range(1, count + 1):
        rng = random.Random(base_rng.randint(0, 2 ** 32))
        variants.append(_generate_one_variant(units, variant_num, rng, seen_orders))
        logger.debug("variant_generated", variant=variant_num, units=len(units))

    logger.info("variants_complete", total=len(variants))
    return variants


def _generate_one_variant(
    units: list[dict[str, Any]],
    variant_num: int,
    rng: random.Random,
    seen_orders: set[tuple],
) -> dict[str, Any]:
    """Build ONE variant from the given units (shared by the single-file flow
    and the Multi-Source Builder — the loop body of generate_variants,
    extracted unchanged)."""
    # Shuffle units (reading groups move as one block)
    order = list(range(len(units)))
    for _ in range(200):
        rng.shuffle(order)
        key = tuple(order)
        if key not in seen_orders:
            seen_orders.add(key)
            break

    shuffled_units = [units[i] for i in order]

    option_mapping: dict[str, dict[str, str]] = {}
    answer_key: dict[str, str | None] = {}
    resolved: list[dict[str, Any]] = []
    pos = 0

    for unit in shuffled_units:
        unit_questions = unit["questions"]

        # For reading groups: keep internal order, only shuffle options
        # For single questions: shuffle options (order already handled at unit level)
        for q in unit_questions:
            pos += 1
            q_id = str(q.get("question_id", q.get("id", uuid.uuid4())))
            shuffled_opts, new_correct = _shuffle_options(q, rng)

            # Build original→new letter map
            orig_opts = q.get("options", {})
            fwd_map: dict[str, str] = {}
            for new_k, text in shuffled_opts.items():
                for orig_k, orig_text in orig_opts.items():
                    if text == orig_text:
                        fwd_map[orig_k] = new_k
                        break

            option_mapping[q_id] = fwd_map
            # answer_key values are LISTS of accepted answers (migration 008).
            #  * MC: the shuffled correct label(s), stored CANONICAL so grading
            #    matches the canonical sheet read. new_correct (from the scalar
            #    correct_answer) is authoritative; EVERY other accepted letter in
            #    correct_answers is mapped through the same shuffle (fwd_map) and
            #    added — a multi-accept MC key ("A / B") must not be dropped.
            #  * WRITTEN/open (no options, not shuffled): the question's accepted
            #    answers as-is (text, multi-accept). Empty → None (ungraded).
            if orig_opts:
                labels = [new_correct] if new_correct else []
                for lab in (q.get("correct_answers") or []):
                    lab = str(lab).strip()
                    if lab and lab in fwd_map and fwd_map[lab] not in labels:
                        labels.append(fwd_map[lab])
                canon: list[str] = []
                for lab in labels:
                    c = canonical_letter(lab)
                    if c not in canon:
                        canon.append(c)
                answer_key[str(pos)] = canon or None
            else:
                accepted = [str(a) for a in (q.get("correct_answers") or []) if str(a).strip()]
                answer_key[str(pos)] = accepted or None

            resolved.append({
                **q,
                "position_in_variant": pos,
                "options": shuffled_opts,
                "correct_answer": new_correct,
                # Only the first question in a reading group carries context
                # so the PDF prints the passage once before the block.
                "group_context": unit["group_context"] if q is unit["questions"][0] else None,
            })

    return {
        "variant_number": variant_num,
        "question_order": [
            str(q.get("question_id", q.get("id", "")))
            for unit in shuffled_units
            for q in unit["questions"]
        ],
        "option_mapping": option_mapping,
        "answer_key": answer_key,
        "questions_data": resolved,
    }


# ── Multi-Source Builder: pool assembly & selection ─────────────────────────────

def assemble_pool(
    questions_by_source: list[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[list], list[list]]:
    """
    Merge per-file question lists into one pool. Every question is tagged
    with source_index; keys travel on the question dicts themselves (loaded
    from each file's own DB rows), so original-numbering collisions between
    files cannot touch answers (P6 — pool identity is the question_id UUID).

    Cross-file EXACT duplicates are collapsed (keep the first-seen copy,
    both sources credited — P4); siblings are kept and reported.

    Returns (pool, collapsed_pairs, sibling_groups) where collapsed_pairs is
    [[kept_source, kept_number, dup_source, dup_number], ...] (1-based
    source indexes) and sibling_groups is [[source, number], ...] lists.
    """
    from app.services.ai_analyzer import question_fingerprint

    pool: list[dict[str, Any]] = []
    seen_fp: dict[str, dict] = {}
    collapsed: list[list] = []

    for src_idx, questions in enumerate(questions_by_source, start=1):
        for q in questions:
            q = {**q, "source_index": src_idx, "section": 1}
            fp = question_fingerprint(q)
            kept = seen_fp.get(fp)
            if kept is not None and fp.replace(":", ""):
                kept.setdefault("source_indexes", [kept["source_index"]])
                if src_idx not in kept["source_indexes"]:
                    kept["source_indexes"].append(src_idx)
                collapsed.append([
                    kept["source_index"], kept.get("question_number", 0),
                    src_idx, q.get("question_number", 0),
                ])
                logger.info(
                    "pool_duplicate_collapsed",
                    kept=(kept["source_index"], kept.get("question_number")),
                    dropped=(src_idx, q.get("question_number")),
                )
                continue
            seen_fp[fp] = q
            pool.append(q)

    # Siblings across the pool (same stem, different content) — info only
    from app.services.ai_analyzer import find_siblings
    siblings = [
        [src_num for src_num in nums]
        for _sec, nums in find_siblings(pool)
    ]
    return pool, collapsed, siblings


def predicted_reuse(pool_size: int, n_variants: int, m_per_variant: int) -> int:
    """P8: upper bound on how many times a single question will be reused."""
    if pool_size <= 0:
        return 0
    import math
    return math.ceil((n_variants * m_per_variant) / pool_size)


def select_for_variants(
    questions: list[dict[str, Any]],
    n_variants: int,
    m_per_variant: int,
    seed: int | None = None,
) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    """
    Choose M questions for each of N variants from the pool.

    Rules:
    - group questions are atomic units; the whole group is taken and its
      size counts toward M (a unit that would overshoot M is skipped)
    - least-used units first, random among ties → reuse is minimized and
      spread evenly when the pool is smaller than N×M
    - no duplicate content within one variant (unit identity + fingerprint)

    Returns (selections, stats) where stats carries max_reuse, reused unit
    previews and any per-variant shortfalls (M unreachable exactly).
    """
    from app.services.ai_analyzer import question_fingerprint

    units = _build_units(questions)
    if not units:
        raise ValueError("Cannot select from an empty pool")
    unit_fps = [
        {question_fingerprint(q) for q in u["questions"]} for u in units
    ]
    usage = [0] * len(units)
    rng = random.Random(seed)

    selections: list[list[dict[str, Any]]] = []
    shortfalls: list[tuple[int, int]] = []  # (variant_number, got)

    for v in range(1, n_variants + 1):
        # Greedy least-used-first can strand itself on group-size arithmetic
        # (all singles taken, remaining slot smaller than the group) — retry
        # with fresh tie-randomization and keep the best attempt; usage is
        # committed only for the accepted attempt.
        best_idx: list[int] = []
        best_got = -1
        for _attempt in range(6):
            remaining = m_per_variant
            chosen_idx: list[int] = []
            chosen_fps: set[str] = set()
            candidates = sorted(
                range(len(units)), key=lambda i: (usage[i], rng.random())
            )
            for i in candidates:
                size = len(units[i]["questions"])
                if size > remaining:
                    continue
                if chosen_fps & unit_fps[i]:
                    continue  # identical content already in this variant
                chosen_idx.append(i)
                chosen_fps |= unit_fps[i]
                remaining -= size
                if remaining == 0:
                    break
            got = m_per_variant - remaining
            if got > best_got:
                best_got, best_idx = got, chosen_idx
            if remaining == 0:
                break

        for i in best_idx:
            usage[i] += 1
        if best_got < m_per_variant:
            shortfalls.append((v, best_got))
            logger.warning(
                "variant_selection_shortfall",
                variant=v, wanted=m_per_variant, got=best_got,
            )
        selections.append([
            q for i in best_idx for q in units[i]["questions"]
        ])

    reused = [
        units[i]["questions"][0].get("question_number", 0)
        for i in range(len(units)) if usage[i] > 1
    ]
    stats = {
        "max_reuse": max(usage) if usage else 0,
        "reused_count": len(reused),
        "reused_numbers": sorted(reused)[:20],
        "shortfalls": shortfalls,
    }
    return selections, stats


def pool_variant_builder(
    selections: list[list[dict[str, Any]]],
    seed: int | None = None,
):
    """
    Return (total, build_one) where build_one(i) constructs the i-th variant
    (1-based) from its pre-selected question list, through the SAME machinery
    as the single-file flow (validation + _generate_one_variant).

    The builder is a pure, fast, SYNC closure holding the shared rng/seen_orders
    state — callers drive the loop so they can add per-variant progress and a
    per-variant timeout (generation must never hang silently).
    """
    base_rng = random.Random(seed)
    seen_orders: set[tuple] = set()

    def build_one(i: int) -> dict[str, Any]:
        selection = selections[i - 1]
        valid, rejected = validate_questions(selection)
        if rejected:
            logger.warning(
                "pool_variant_rejects", variant=i,
                rejected=[r["question_number"] for r in rejected],
            )
        if not valid:
            raise ValueError(f"Variant {i}: no valid questions selected")
        units = _build_units(valid)
        rng = random.Random(base_rng.randint(0, 2 ** 32))
        return _generate_one_variant(units, i, rng, seen_orders)

    return len(selections), build_one


def generate_pool_variants(
    selections: list[list[dict[str, Any]]],
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Build all variants at once (used by tests and any synchronous caller)."""
    total, build_one = pool_variant_builder(selections, seed)
    variants = [build_one(i) for i in range(1, total + 1)]
    logger.info("pool_variants_complete", total=len(variants))
    return variants
