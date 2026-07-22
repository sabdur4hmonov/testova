"""
AI question extractor — Gemini Vision, 1 page per call, parallel.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import google.generativeai as genai
from PIL import Image

from app.config import settings
from app.services.ocr_corrections import apply_ocr_corrections
from app.utils.logging import get_logger

logger = get_logger(__name__)

genai.configure(api_key=settings.GEMINI_API_KEY)

MAX_CONCURRENT = 4   # parallel Gemini calls
CALL_TIMEOUT   = 90  # seconds
MAX_CONTINUATIONS = 3  # extra same-page calls when output hits the token cap

# Appended to VISION_PROMPT when a page's output was truncated: fetch the
# rest of the SAME page. Positional ("after ... in reading order"), so it
# works even when a new section restarts numbering mid-page.
CONTINUATION_NOTE = """

IMPORTANT — CONTINUATION PASS: A previous pass already extracted this page's
questions up to and including question number {last}. Extract ONLY the
questions that appear AFTER question {last} on this page (later in reading
order) — including the questions of a NEW section/test if one starts there.
Do NOT re-extract earlier questions."""

# ── Prompt ──────────────────────────────────────────────────────────────────────
VISION_PROMPT = """You are a professional exam question extractor. Extract ALL questions from this test page image.
Return ONLY a valid JSON array. No markdown, no code blocks, no explanation. ONLY JSON.

Format:
[
  {
    "n": 1,
    "q": "full question text exactly as written",
    "opts": {"A": "option A text exactly as written", "B": "option B text exactly as written", "C": "option C text exactly as written", "D": "option D text exactly as written"},
    "ans": null,
    "img": false,
    "img_desc": null,
    "group": null,
    "group_context": null,
    "section": null,
    "verbatim_doubt": false
  }
]

CRITICAL RULES:
1. Extract ALL questions visible on this page, do not skip any
2. Keep the original question number (n) exactly as shown
3. Copy ALL text EXACTLY as written - do not change +/- signs, do not modify equations
4. For equations: copy character by character. If you see 3(x+1) write 3(x+1), NOT 3(x-1)
5. If a question has an image/diagram/table: set img=true, describe it in img_desc
6. For answer options ("opts"): use the EXACT label printed before each option
   as the key, in the order printed. Labels may be Latin (A, B, C, D, E) OR
   Cyrillic (А, Б, В, Г, Д, Е). Copy each option's TEXT exactly as written.
   - Do NOT renumber or relabel. Do NOT convert Cyrillic labels to Latin.
   - Do NOT fill gaps: a paper printed "a) b) d) e)" returns keys "a","b","d","e"
     with NO "c". A paper with five options returns five keys.
   - Options may run INLINE on one line, and an option's TEXT may itself contain
     inner numbering like "1)...2)..." (e.g. "a) 1)14sm,2)48sm; b) 1)15sm...").
     That inner numbering is part of the option TEXT — it is NOT a label. Key
     each option by its printed marker (a, b, d, e...) exactly, and NEVER
     renumber them into a fresh A,B,C,D sequence.
   - "ans" (if you can see the marked/correct option) must be one of these exact
     labels.
7. If answer options are NOT visible on this page (cut off), return "opts": {}
8. Do NOT invent or guess missing options - return "opts": {} when there are none
9. Do NOT use LaTeX or $ symbols. Use ONE consistent plain-text notation for
   ALL formulas (never mix styles inside a document):
   - Fractions: write as (a)/(b) example: (1)/(2)
   - Mixed numbers (a whole number next to a fraction, e.g. 3½): write as
     "3 1/2" WITH A SPACE between the whole part and the fraction. NEVER write
     it as 3(1)/(2) — that reads as 3·(1/2)=1.5 and changes the value. So
     3½ = "3 1/2", 2¾ = "2 3/4".
   - Powers: write as x^2, 2^21, x^n. Keep the WHOLE exponent: "2^21" means 2
     to the power 21 — never drop or split its digits.
   - Subscripts (a small LOWERED index on a variable): write base_index with an
     underscore. x with subscript 1 → x_1, x with subscript 2 → x_2, a_n, S_k.
     ALWAYS use the underscore — NEVER glue as "x1"/"x2" (that reads as the
     number x1). Worked example: the two roots of an equation are "x_1" and
     "x_2", e.g. "x_1 + x_2 = 5" and "4x_1 + 3x_2 = 3 1/2".
   - Repeating (periodic) decimals in the Uzbek "a,(b)" notation, e.g. 4,(2)
     = 4.222..., 0,(45) = 0.4545...: copy them EXACTLY as "4,(2)". This is a
     DECIMAL, NOT a power — never turn "4,(2)" into "4^(2)", "4^2" or "4²".
   - Square root: ALWAYS write as sqrt(...) in ASCII. Never use the "√"
     character. The radicand is EXACTLY what sits UNDER the bar — no more, no
     less. This works in BOTH directions:
     * a term that IS under the bar must not escape it: if the bar covers
       "2sqrt(x) - sqrt(3x)", write sqrt(2sqrt(x) - sqrt(3x)) - NEVER let a
       term like "- sqrt(3x)" fall outside the sqrt.
     * a term that is OUTSIDE the bar must NOT be pulled inside it: "4√3 + 2"
       means 4·√3 plus 2 — the "+ 2" is a SEPARATE TERM, outside the radical.
       Write it as 4sqrt(3) + 2 — NEVER as 4sqrt(3 + 2), and NEVER as
       4sqrt(sqrt(3) + 2).
   - nth root (cube, fourth, ...): write as root(n, expression), e.g. the
     fourth root of x → root(4, x); the cube root of (a+b) → root(3, a+b).
     The small index n is NOT an exponent: NEVER turn a fourth root into "x^4"
     or "2^(4...)". The index is the first argument of root(...) and the
     ENTIRE radicand stays inside its parentheses. Example: the fourth root of
     "x·(7 + 4sqrt(3))" is root(4, x*(7 + 4sqrt(3))) - the (7 + 4sqrt(3))
     factor stays INSIDE the root, never beside it.
   - π symbol: write as π (the actual symbol)
   - Infinity: write as ∞
   - Set/math symbols: preserve as REAL Unicode characters exactly as printed:
     ∈ ∉ ∅ ⊂ ⊆ ∪ ∩ ℝ ℕ ℤ ℚ ≤ ≥ ≠ ≈ ± °
     Example: "n ∈ N" must stay "n ∈ N", never "n □ N" or "n ? N"
   - Reaction/process arrows with conditions written above/below the arrow
     (any subject: chemistry chains, physics processes, biology cycles):
     put the condition in parentheses right after the arrow.
     Example: "X →(t°) Y →(catalyst) Z"
     NEVER skip a question because its notation is hard to format -
     extract it using this convention
   - Isotope/nuclide notation: ALWAYS write as ^A_Z Symbol,
     e.g. ^56_26 Fe, ^254_102 No, ^4_2 α.
     NEVER glue the numbers together: "254102No" or "4ZE" is WRONG
   - Bond symbols in structural formulas: preserve = (double) and
     ≡ (triple) exactly as printed
   - NEVER output □ (a box) or ? in place of a symbol you can see - if you
     recognize the symbol, output the proper Unicode character for it
10. Keep Uzbek and Russian text exactly as is, do not translate
11. IMPORTANT: Some questions may have NO answer options at all (open-ended questions).
    If a question genuinely has no A/B/C/D options anywhere on the page, leave all as "".
12. SHARED PASSAGES / READING GROUPS: Some questions share a common passage, text,
    table, dialogue, or instruction that applies to several consecutive questions
    (e.g. "Read the text and answer questions 4-6", or a paragraph followed by
    several questions about it).
    - For EVERY question that belongs to the same shared block, set "group" to the
      SAME short label (e.g. "g1" for the first block, "g2" for the second).
    - Put the shared passage/instruction text in "group_context" - the SAME text
      for each question in that block.
    - For a normal standalone question with no shared passage, set both "group"
      and "group_context" to null.
13. PAGE-BREAK CONTINUATIONS: A page may start with the continuation of a question
    from the PREVIOUS page (an answer-options block without a question stem, or
    question text without a question number).
    IMPORTANT: headers, footers and decorative lines are NOT question content.
    When deciding whether the page starts with a continuation, SKIP OVER lines
    such as:
    - exam/section title lines (often centered, ALL CAPS)
    - author/compiler/source footer lines (e.g. "Tuzuvchi: ...", "Author: ...")
    - channel/website/contact mentions, @usernames, links
    - page numbers, dates, horizontal rules
    If the FIRST REAL QUESTION CONTENT on the page (after skipping such lines)
    is an options block or unnumbered continuation text, return it as the
    FIRST array item:
    - "n": 0  (do NOT guess the real question number)
    - "q": the continuation text, or "" if the block is only answer options
    - put any visible options in A/B/C/D exactly as written
    Do NOT skip such orphaned blocks, do NOT attach them to another question,
    and NEVER treat a header/title/footer line as a question or a question stem.
14. MULTIPLE TESTS IN ONE DOCUMENT: A document may contain several independent
    tests/sections, each restarting its numbering from 1, usually introduced by
    a section title line (e.g. "Anorganik moddalarning eng muhim sinflari").
    If a NEW section/test title appears on this page and question numbering
    restarts after it, set "section" to that title text on the FIRST question
    of the new section. For all other questions set "section" to null.
    Keep each question's printed number exactly as shown - do NOT renumber.
15. TWO-COLUMN PAGES: If the page is laid out in TWO columns, read the LEFT
    column completely top-to-bottom FIRST, then the RIGHT column top-to-bottom.
    NEVER interleave lines or questions across columns. Return questions in
    that reading order.
16. MULTIPLE REACTIONS: If a question contains SEVERAL reactions/equations,
    transcribe ALL of them, each on its own line. Never keep only the first
    one - a question asking about X is unanswerable without the reaction
    that defines X.
17. VERBATIM GUARANTEE: transcribe formulas and numbers character-for-character
    exactly as printed. If something looks wrong, impossible or cut off,
    COPY IT AS-IS and set "verbatim_doubt": true for that question.
    NEVER repair, complete or normalize a formula.
18. IMAGE KEYWORDS: the Uzbek words "rasmda", "rasmga", "jadvalda",
    "diagrammada", "grafikda", "shakl", "ko'rsatilgan", "tasvirlangan",
    "sxemada" mean the question refers to a picture/table/diagram/graph — when
    the stem contains any of them, set img=true.
19. RETURN ONLY THE JSON ARRAY - nothing else"""

ANSWER_SHEET_PROMPT = """Bu o'quvchining javob varaqasi. Test {total} ta savol.
Har savol uchun belgilangan javobni o'qi (A/B/C/D), bo'sh bo'lsa null.
FAQAT JSON: {{"answers": {{"1": "A", "2": null}}}}"""

# FIX 7: appended to RECOVER_QUESTIONS_PROMPT when re-extracting questions
# the suspicious-content heuristics flagged.
REEXTRACT_STRICT_NOTE = """

STRICT SYMBOL RULES for this pass:
- preserve = (double bond) and ≡ (triple bond) symbols EXACTLY as printed
- preserve superscripts/subscripts; isotopes as ^A_Z Symbol (e.g. ^56_26 Fe)
- copy every formula character-by-character; never normalize or simplify
- Roman numerals in parentheses like (II) must stay (II)
- transcribe ALL reactions/equations a question contains, each on its own line
- if something looks wrong or cut off, COPY IT AS-IS and set
  "verbatim_doubt": true - NEVER repair or complete it"""

# FIX 3(b): transcribe an unreadable/uncroppable scheme into a text chain.
TRANSCRIBE_SCHEME_PROMPT = """Question {n} on this test page contains a scheme/diagram of transformations
(compounds or states connected by arrows, possibly branching).

Transcribe that scheme as PLAIN TEXT:
- start item, then each step as →(reagent/condition)
- if the scheme branches into parallel paths, label them on separate lines:
  "Yuqori yo'l: ..." (top path) and "Pastki yo'l: ..." (bottom path)
- keep every formula/symbol EXACTLY as printed, as real Unicode

If the figure is a TABLE: put each row VERBATIM into "chain", one line per
row, formatted "1. <first cell>; <second cell>". Never merge or reorder
cells, never describe the table in prose.

Return ONLY JSON: {{"chain": "...", "desc": "..."}}
- "chain": the transcribed text chain (or verbatim table rows), else ""
- "desc": one-sentence description that INCLUDES the actual printed
  compounds/reagents, else ""
NEVER invent compounds or reagents that are not printed on the page."""

# Targeted retry for question numbers that are ENTIRELY missing after
# extraction (typically dense content: reaction chains, diagrams, tables).
RECOVER_QUESTIONS_PROMPT = """A previous extraction pass MISSED these questions from the attached test page image(s): {nums}

Find EACH of these question numbers on the pages and extract them COMPLETELY.
They may be visually complex: chemical reaction chains with conditions written
above/below arrows, diagrams, tables, or multi-line equations.

Return ONLY a JSON array, one item per found question:
[{{"n": 33, "q": "full question text", "A": "...", "B": "...", "C": "...", "D": "...", "img": false, "img_desc": null}}]

CRITICAL RULES:
- Copy text EXACTLY as printed; keep math/science symbols as real Unicode
- Reaction/process arrows with conditions above/below: write the condition
  in parentheses right after the arrow: "X →(t°) Y →(catalyst) Z"
- If a question has an image/diagram, set img=true and describe it in img_desc
- Extract ONLY the question numbers listed above - no others
- If you cannot find a question number on these pages, simply OMIT it
- NEVER invent or reconstruct content that is not printed on the pages
- RETURN ONLY THE JSON ARRAY - nothing else"""

# BUG FIX (#1): targeted second pass for questions extracted with zero options.
RECOVER_OPTIONS_PROMPT = """These test-page image(s) contain questions whose answer options were NOT captured
in a previous extraction pass.
Question numbers to recover: {nums}

For EACH of these question numbers, look for its printed answer options
(A/B/C/D, sometimes E). The options may be detached from the question stem:
after an image or table, at the bottom of the first page, or at the top of
the second page (after header/footer lines).

Return ONLY a JSON array, one item per question number:
[{{"n": 20, "A": "option text", "B": "...", "C": "...", "D": "..."}}]

CRITICAL RULES:
- Copy option text EXACTLY as printed; keep math symbols (∈ ∅ π √ ...) as real Unicode
- Use the EXACT printed marker as each option's label — keep gaps (a,b,d,e stays
  a,b,d,e, no "c") and keep Cyrillic labels as-is. Inner numbering inside an
  option's text ("1)...2)...") is part of the TEXT, not a label. NEVER renumber
  the options into a fresh A,B,C,D sequence.
- If a question genuinely has NO printed options anywhere on these pages,
  return "" for all its letters
- NEVER invent, guess or complete options that are not printed on the pages
- RETURN ONLY THE JSON ARRAY - nothing else"""


# ── LaTeX cleanup ────────────────────────────────────────────────────────────────

def clean_latex(text: str) -> str:
    """
    Remove LaTeX math notation and convert to plain readable text.
    Applied to all question text and answer options after Gemini returns them.
    """
    if not text:
        return text

    # \frac{a}{b} -> (a)/(b)
    for _ in range(4):
        text = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1)/(\2)', text)

    # \sqrt{x} -> sqrt(x)
    text = re.sub(r'\\sqrt\{([^{}]+)\}', r'sqrt(\1)', text)

    # \sqrt[n]{x} -> nth_root(x)
    text = re.sub(r'\\sqrt\[([^\]]+)\]\{([^{}]+)\}', r'\1_root(\2)', text)

    # \cdot -> *
    text = text.replace(r'\cdot', ' * ')

    # \times -> x
    text = text.replace(r'\times', ' x ')

    # \div -> /
    text = text.replace(r'\div', ' / ')

    # \leq -> <=  \geq -> >=  \neq -> !=
    text = text.replace(r'\leq', '<=')
    text = text.replace(r'\geq', '>=')
    text = text.replace(r'\neq', '!=')
    text = text.replace(r'\ne',  '!=')

    # \infty -> ∞
    text = text.replace(r'\infty', '∞')

    # \pi -> π
    text = text.replace(r'\pi', 'π')

    # Greek letters
    greek = {
        r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\delta': 'δ',
        r'\epsilon': 'ε', r'\theta': 'θ', r'\lambda': 'λ', r'\mu': 'μ',
        r'\sigma': 'σ', r'\omega': 'ω', r'\phi': 'φ', r'\psi': 'ψ',
    }
    for latex_sym, unicode_sym in greek.items():
        text = text.replace(latex_sym, unicode_sym)

    # Set symbols
    text = text.replace(r'\in',    '∈')
    text = text.replace(r'\notin', '∉')
    text = text.replace(r'\cup',   '∪')
    text = text.replace(r'\cap',   '∩')
    text = text.replace(r'\subset','⊂')

    # ^{...} and _{...}
    text = re.sub(r'\^\{([^{}]+)\}', r'^(\1)', text)
    text = re.sub(r'_\{([^{}]+)\}',  r'_(\1)', text)

    # Simple ^x and _x
    text = re.sub(r'\^([A-Za-z0-9])', r'^\1', text)
    text = re.sub(r'_([A-Za-z0-9])',  r'_\1', text)

    # Remove $...$ inline math markers
    text = re.sub(r'\$\$([^$]+)\$\$', r'\1', text)
    text = re.sub(r'\$([^$]+)\$',     r'\1', text)

    # Remove \begin{...} \end{...}
    text = re.sub(r'\\begin\{[^}]+\}', '', text)
    text = re.sub(r'\\end\{[^}]+\}',   '', text)

    # Remove remaining backslash commands
    text = re.sub(r'\\text\{([^{}]+)\}', r'\1', text)
    text = re.sub(r'\\[a-zA-Z]+\b', '', text)

    # Targeted lexical repair: Gemini sometimes glues an element symbol to
    # the following Uzbek word "tutgan" ("Fe tutgan" → "Fetutgan"). A general
    # "unglue words" rule would corrupt formulas, so only this word is fixed.
    text = re.sub(r'([A-Za-z0-9\)])tutgan\b', r'\1 tutgan', text)

    # Clean up extra whitespace
    text = re.sub(r'  +', ' ', text)
    return text.strip()


# Uzbek words that signal a question refers to a visual (picture/table/graph).
# Safety net: if Gemini missed img=true but the stem uses one of these, force
# has_image=True so the figure pipeline runs.
_IMAGE_KEYWORDS = (
    "rasmda", "rasmga", "jadvalda", "diagrammada", "grafikda",
    "shakl", "ko'rsatilgan", "koʻrsatilgan", "tasvirlangan", "sxemada",
)


def _force_image_from_keywords(q: dict) -> None:
    if q.get("has_image"):
        return
    stem = (q.get("question_text") or "").lower()
    if any(k in stem for k in _IMAGE_KEYWORDS):
        q["has_image"] = True
        logger.info("image_keyword_forced", question=q.get("question_number"))


def clean_question(q: dict) -> dict:
    """Apply clean_latex + the OCR confusion dictionary to all text fields."""
    q["question_text"] = clean_latex(q.get("question_text", ""))
    opts = q.get("options", {})
    q["options"] = {k: clean_latex(v) for k, v in opts.items() if v}
    # Shape guard: a description that is a sentence ABOUT whether an image exists
    # (a leaked prompt answer) is not a real description — drop it so it can never
    # reach the printed "[Rasm]: ..." box. Applies to EVERY extraction path.
    if _is_meta_desc(q.get("image_description")):
        logger.info("meta_desc_dropped", question=q.get("question_number"))
        q["image_description"] = None
    if q.get("image_description"):
        q["image_description"] = clean_latex(q["image_description"])

    # ISSUE 5: whole-word OCR corrections, every replacement logged
    all_repl: list[tuple[str, str, int]] = []
    q["question_text"], r = apply_ocr_corrections(q["question_text"])
    all_repl += r
    fixed_opts = {}
    for k, v in q["options"].items():
        fixed_opts[k], r = apply_ocr_corrections(v)
        all_repl += r
    q["options"] = fixed_opts
    if q.get("image_description"):
        q["image_description"], r = apply_ocr_corrections(q["image_description"])
        all_repl += r
    if all_repl:
        q["ocr_fixes"] = q.get("ocr_fixes", 0) + sum(n for _, _, n in all_repl)
        logger.info(
            "ocr_corrections_applied",
            question=q.get("question_number"),
            replacements=all_repl,
        )
    _force_image_from_keywords(q)
    return q


def summarize_sections(
    questions: list[dict],
    removed: set[tuple[int, int]] | None = None,
) -> list[dict]:
    """
    Multi-test documents: describe each detected section WITHOUT touching the
    questions (no merging, no renumbering — the teacher picks ONE section and
    the others are discarded; combining tests is a separate future feature).

    removed: {(section, number)} registry of questions deliberately removed
    (exact duplicates). A number is only reported "missing" (gap) if it was
    never extracted AND not removed — a deduped question must never alarm
    the teacher as missing.

    Returns JSON-safe meta per section:
    {"section", "title", "count", "max",
     "gaps": [numbers missing in 1..max], "open": [open-ended numbers]}
    """
    removed = removed or set()
    by_section: dict[int, list[dict]] = {}
    for q in questions:
        by_section.setdefault(q.get("section", 1), []).append(q)

    sections_meta: list[dict] = []
    for sec in sorted(by_section):
        qs = by_section[sec]
        nums = [q["question_number"] for q in qs if q.get("question_number")]
        max_n = max(nums) if nums else 0
        present = set(nums)
        sections_meta.append({
            "section": sec,
            "title": next(
                (q.get("section_title") for q in qs if q.get("section_title")),
                None,
            ),
            "count": len(nums),
            "max": max_n,
            "gaps": [
                x for x in range(1, max_n + 1)
                if x not in present and (sec, x) not in removed
            ],
            "open": [q["question_number"] for q in qs if q.get("is_open_ended")],
        })
    return sections_meta


# ── Scheme validation (FIX 2) ───────────────────────────────────────────────

# Trigger phrases indicating the stem refers to a scheme the student must see.
# Extensible list — general triggers (has_image / image_description) apply
# regardless of language; the phrases are belt-and-braces for unflagged stems.
SCHEME_TRIGGER_PHRASES = (
    "o'zgarishlar asosida",
    "oʻzgarishlar asosida",
    "quyidagi sxema",
    "quyidagi o'zgarish",
)

# Crude chemical/scientific formula detector: "CuSO4", "KOH", "Al4C3", "H2O"
FORMULA_RE = re.compile(r'\b[A-Z][a-z]?\d|\b(?:[A-Z][a-z]?){2,}\b')

# Descriptions that carry no content and must never reach the printed PDF.
_USELESS_DESC_RE = re.compile(r'cut ?off|is cut|kesilgan|not (?:visible|readable)', re.I)

# A recovery/transcription call can return PROSE answering the prompt's implicit
# question ("does this question contain a scheme?") instead of a description OF a
# figure — e.g. "Question 7 does not contain a scheme/diagram of transformations".
# That is a sentence ABOUT whether an image exists, not a description of one; it
# must NEVER be stored as image_description (it renders as a "[Rasm]: ..." box on
# the exam). Shape guard — reject the tell-tale meta phrasings.
_META_DESC_RE = re.compile(
    r"\b(?:does\s*not|does\s*n.t|do\s*not|don.t|did\s*not|didn.t)\s+"
    r"(?:contain|have|include|show|depict)\b"
    r"|\bthere\s+is\s+no\b"
    r"|\bno\s+(?:scheme|diagram|transformation)\b"
    r"|^\s*question\s+\d+\b",
    re.I,
)


def _is_meta_desc(desc: str | None) -> bool:
    """True if `desc` is a sentence ABOUT whether an image exists (a leaked
    prompt answer), not a description OF an image — must not be stored."""
    return bool(desc and _META_DESC_RE.search(desc))


def _needs_scheme(q: dict) -> bool:
    """Does this question require visible scheme content to be answerable?"""
    if q.get("has_image") or q.get("image_description"):
        return True
    stem = (q.get("question_text") or "").lower()
    return any(p in stem for p in SCHEME_TRIGGER_PHRASES)


def _desc_redundant(stem: str | None, desc: str | None) -> bool:
    """ISSUE 4(a): the stem carries the full chain ("→" present) and >=90%
    of the description's formula/entity tokens already appear in the stem —
    the [Rasm] box would only restate the chain."""
    stem = stem or ""
    if not desc or "→" not in stem:
        return False
    tokens = set(FORMULA_RE.findall(desc)) | set(
        re.findall(r'\b[XYZ][₀-₉0-9]?\b', desc)
    )
    if not tokens:
        return False
    hit = sum(1 for t in tokens if t in stem)
    return hit / len(tokens) >= 0.9


def _has_scheme_content(q: dict) -> bool:
    """Attached image, a text chain with arrows, or a formula-bearing description."""
    if q.get("image_path"):
        return True
    text = q.get("question_text") or ""
    opts = " ".join(str(v) for v in (q.get("options") or {}).values())
    if "→" in text or "→" in opts:
        return True
    desc = q.get("image_description") or ""
    if desc and not _USELESS_DESC_RE.search(desc) and FORMULA_RE.search(desc):
        return True
    return False


# ── De-duplication (FIX 4) ──────────────────────────────────────────────────

# Apostrophe look-alikes teachers'/Gemini's output mixes freely: keeping any
# of them in the fingerprint made "o'zgarish" (U+2019) ≠ "oʻzgarish" (U+02BB)
# and let exact duplicates through. All are DROPPED after NFKC folding.
_APOSTROPHE_RE = re.compile(r"['‘’ʻʼʹ′`´]")


def _fp_norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "")  # ² → 2, ligatures, width folds
    s = _APOSTROPHE_RE.sub("", s.lower())
    # Meaning-bearing symbols SURVIVE normalization: "+2" vs "-2", "2:3" vs
    # "2/3", "=" vs "≡" are different questions, not duplicates. Cosmetic
    # punctuation (.,;?!()) is still stripped.
    return re.sub(r'[^a-zа-яё0-9+\-:/%=<>→≡]', '', s)


def _scheme_key(q: dict) -> str:
    """First compound of the chain, else first formula in the description —
    distinguishes sibling questions that differ only by their scheme."""
    text = q.get("question_text") or ""
    if "→" in text:
        head = text.split("→")[0].split()
        if head:
            return head[-1]
    m = FORMULA_RE.search(q.get("image_description") or "")
    return m.group() if m else ""


def question_fingerprint(q: dict) -> str:
    stem = _fp_norm(q.get("question_text"))
    opts = "|".join(sorted(_fp_norm(str(v)) for v in (q.get("options") or {}).values()))
    return f"{stem}::{opts}::{_fp_norm(_scheme_key(q))}"


def find_exact_duplicates(questions: list[dict]) -> list[dict]:
    """
    CHANGE 2: detection ONLY — nothing is removed here. Questions whose
    fingerprints match exactly within a section form a group; the TEACHER
    decides once/twice AFTER entering the full answer key.

    Returns [{"section": s, "numbers": [15, 35], "preview": "stem..."}].
    """
    by_fp: dict[tuple[int, str], list[dict]] = {}
    for q in questions:
        by_fp.setdefault(
            (q.get("section", 1), question_fingerprint(q)), []
        ).append(q)

    groups: list[dict] = []
    for (sec, fp), qs in by_fp.items():
        if len(qs) < 2 or not fp.replace(":", ""):
            continue
        nums = sorted(q.get("question_number", 0) for q in qs)
        groups.append({
            "section": sec,
            "numbers": nums,
            "preview": (qs[0].get("question_text") or "")[:80],
        })
        logger.info("exact_duplicates_detected", section=sec, numbers=nums)
    groups.sort(key=lambda g: g["numbers"])
    return groups


def find_siblings(questions: list[dict]) -> list[tuple[int, list[int]]]:
    """Same stem but DIFFERENT content (options/scheme) — legitimate sibling
    questions (KOH/NaOH variants). Info for the teacher only."""
    stems: dict[tuple[int, str], list[dict]] = {}
    for q in questions:
        key = (q.get("section", 1), _fp_norm(q.get("question_text")))
        if key[1]:
            stems.setdefault(key, []).append(q)
    out: list[tuple[int, list[int]]] = []
    for (sec, _), qs in stems.items():
        if len(qs) > 1 and len({question_fingerprint(q) for q in qs}) > 1:
            out.append((sec, sorted(q.get("question_number", 0) for q in qs)))
    return out


def collapse_sections(questions: list[dict]) -> list[dict]:
    """
    CHANGE 1 borderline case: a LOW-CONFIDENCE section split is treated as
    detection noise — everything folds back to one test (the literal
    pre-section-detection behavior): section 1 for all, same-number
    occurrences merged fill-missing-options, first occurrence wins.
    """
    merged: dict[int, dict] = {}
    for q in questions:
        q["section"] = 1
        q["section_title"] = None
        n = q.get("question_number", 0)
        if n in merged:
            prim = merged[n]
            for letter, v in (q.get("options") or {}).items():
                if v and not (prim.get("options") or {}).get(letter):
                    prim.setdefault("options", {})[letter] = v
            continue
        merged[n] = q
    out = sorted(merged.values(), key=lambda x: x.get("question_number", 0))
    logger.info(
        "sections_collapsed",
        kept=len(out), merged_away=len(questions) - len(out),
    )
    return out


def sections_confident(sections_meta: list[dict]) -> bool:
    """CHANGE 1: refuse a multi-section file only on CONFIDENT detection —
    at least two sections, each carrying >= 4 questions. A tiny "section"
    is extraction noise, not a second test."""
    return (
        len(sections_meta) > 1
        and all(m.get("count", 0) >= 4 for m in sections_meta)
    )


def find_near_duplicates(
    questions: list[dict], threshold: float = 0.9
) -> list[tuple[int, list[int]]]:
    """
    FIX 5(c): after exact dedup, find SUSPECTED duplicates — identical
    sorted-normalized option sets AND stem similarity >= threshold.
    Reported to the teacher, never auto-deleted (small wording differences
    can hide genuinely different questions).
    """
    by_opts: dict[tuple[int, str], list[dict]] = {}
    for q in questions:
        opts_key = "|".join(
            sorted(_fp_norm(str(v)) for v in (q.get("options") or {}).values())
        )
        if not opts_key:
            continue
        by_opts.setdefault((q.get("section", 1), opts_key), []).append(q)

    groups: list[tuple[int, list[int]]] = []
    for (sec, _), qs in by_opts.items():
        if len(qs) < 2:
            continue
        used = [False] * len(qs)
        for i in range(len(qs)):
            if used[i]:
                continue
            cluster = [qs[i]]
            used[i] = True
            a = _fp_norm(qs[i].get("question_text"))
            for j in range(i + 1, len(qs)):
                if used[j]:
                    continue
                b = _fp_norm(qs[j].get("question_text"))
                if a and b and SequenceMatcher(None, a, b).ratio() >= threshold:
                    cluster.append(qs[j])
                    used[j] = True
            if len(cluster) > 1:
                nums = sorted(q.get("question_number", 0) for q in cluster)
                groups.append((sec, nums))
                logger.warning("near_duplicates_suspected", section=sec, numbers=nums)
    return groups


# ── Suspicious-question flagging (FIX 7: flag, never auto-rewrite) ───────────

# Same element twice in one formula token (S2S) — CH3CH3 etc. don't match
# because the repeat must be IMMEDIATELY adjacent (after optional digits).
_REPEATED_ELEMENT_RE = re.compile(r'\b([A-Z][a-z]?)\d*\1\d*\b')
_DANGLING_VA_RE = re.compile(r"\bva\s+hosil\s+bo", re.I)
_RATIO_RE = re.compile(r'\b(\d+(?:\s*:\s*\d+)+)\b')
_FORMULA_TOKEN_RE = re.compile(r'\b(?:[A-Z][a-z]?\d*){2,}\b')
_ELEMENT_RE = re.compile(r'[A-Z][a-z]?')
# Glued isotope digits: "254102No", "y24α" — mass/atomic numbers mashed
_BROKEN_ISOTOPE_RES = (
    re.compile(r'\b\d{4,6}[A-Z][a-z]?\b'),
    re.compile(r'\b[a-z]\d+α'),
)
# Known OCR confusions ("(II)" read as "fill", ...). Extensible.
_OCR_CONFUSION_TOKENS = ("fill",)
# BUG 1: a root INDEX collapsed onto its base as an exponent — Gemini turns
# ⁴√(...) into "2^(4√x)" or "2^4√...", i.e. a "√" that has drifted inside a
# superscript/^(...) group. A legitimate power never contains a radical, so a
# "√" appearing right after "^" (optionally through a "(" ) is a mangled
# nested radical. Flag only — the formula is never auto-rewritten.
_RADICAL_IN_EXPONENT_RE = re.compile(r'\^\(?\s*[^\s()]*√')


def flag_suspicious_questions(
    questions: list[dict],
) -> list[tuple[int, int, str]]:
    """
    Heuristics for content that survived extraction but looks corrupted.
    Returns (section, number, comma-joined reason slugs) — the teacher sees
    the numbers and can trigger a strict re-extraction; nothing is rewritten
    automatically.
    """
    flagged: list[tuple[int, int, str]] = []
    for q in questions:
        stem = q.get("question_text") or ""
        full = " ".join(
            [stem] + [str(v) for v in (q.get("options") or {}).values() if v]
        )
        reasons: list[str] = []

        if _REPEATED_ELEMENT_RE.search(full):
            reasons.append("repeated_element")

        for line in stem.split("\n"):
            ls = line.strip()
            if ls.startswith("=") or ls.endswith("="):
                reasons.append("empty_equation_side")
                break

        if _DANGLING_VA_RE.search(stem):
            reasons.append("dangling_product")

        rm = _RATIO_RE.search(full)
        if rm and "birikma" in full.lower():
            parts = len(rm.group(1).split(":"))
            max_elems = 0
            for ft in _FORMULA_TOKEN_RE.findall(full):
                max_elems = max(max_elems, len(set(_ELEMENT_RE.findall(ft))))
            if max_elems and parts > max_elems:
                reasons.append("ratio_element_mismatch")

        if ("oksidlan" in stem.lower() and re.search(r'\bC?H?\d*\(?CH', stem)
                and "=" not in stem and "≡" not in stem):
            reasons.append("possible_lost_bond")

        low_tokens = set(re.findall(r'[^\W\d_]+', full.lower()))
        if any(tok in low_tokens for tok in _OCR_CONFUSION_TOKENS):
            reasons.append("ocr_confusion")

        if any(rx.search(full) for rx in _BROKEN_ISOTOPE_RES):
            reasons.append("broken_isotope")

        # BUG 1: a fourth/nth-root index mashed into an exponent, with the
        # radical sign dragged inside the power (e.g. "2^(4√x)").
        if _RADICAL_IN_EXPONENT_RE.search(full):
            reasons.append("mangled_radical")

        if reasons:
            flagged.append((
                q.get("section", 1),
                q.get("question_number", 0),
                ",".join(reasons),
            ))
            logger.info(
                "suspicious_question",
                question=q.get("question_number"), reasons=reasons,
            )
    return flagged


# ── Post-extraction stem cleaning (ISSUES 1, 4b, 4d) ─────────────────────────

def _strip_own_number(q: dict) -> None:
    """
    ISSUE 1: strip a leading "N." / "N)" / "N ." token from the stem — ONLY
    the question's own known number N. Generic digits are never touched, so
    a legitimate list marker like "1)" inside a stem survives.
    """
    n = q.get("question_number")
    if not n:
        return
    text = q.get("question_text") or ""
    # Tolerate a SHORT leading run of markdown/code artifacts Gemini occasionally
    # leaks before the number — a stray backtick, a ``` fence, ** , etc. Bounded
    # to 0-4 such chars so it can never eat into real stem text, and still gated
    # on {n}[.)], so this strips ONLY the question's OWN number: a list marker
    # "1)" (1 != n) is left intact.
    new = re.sub(rf'^\s*[`*~_#]{{0,4}}\s*{n}\s*[.)]\s*', '', text, count=1)
    if new != text:
        q["question_text"] = new
        logger.info("own_number_stripped", question=n)


def _strip_leading_backticks(q: dict) -> None:
    """Drop a leading run of stray backticks (markdown / code-fence bleed) when
    they open the stem with no number after — the number+markdown case is already
    handled by _strip_own_number. The prompt forbids markdown, so a leading
    backtick is never real stem content. Scoped to BACKTICKS only — unlike a
    leading `*`/`_` (which could be intentional math/emphasis), a leading
    backtick is always an artifact, so the whole leading run is dropped. Runs
    AFTER _strip_own_number, so it only touches the residual no-number case
    (e.g. "`Hisoblang:" -> "Hisoblang:")."""
    text = q.get("question_text") or ""
    new = re.sub(r'^\s*`+\s*', '', text, count=1)
    if new != text:
        q["question_text"] = new
        logger.info("leading_backticks_stripped", question=q.get("question_number"))


_UNKNOWN_NODE = r'[XYZ][₀-₉0-9]*'


def canonicalize_chain_text(text: str | None) -> str | None:
    """
    ISSUE 4(b): one canonical arrow-reagent format. In a transformation CHAIN
    (>= 2 arrows AND unknown nodes like X₁/X₂), a reagent that drifted to the
    node's side ("→ X₁ + NaPO₃ → X₂") is moved into the following arrow
    ("→ X₁ →(+NaPO₃) X₂"). A two-operand FIRST segment becomes
    "SiS₂ →(H₂O) X₁". Gated on the chain signature so genuine
    "A + B → C" equations are never rewritten.
    """
    if not text or text.count("→") < 2:
        return text
    if len(re.findall(rf'\b{_UNKNOWN_NODE}\b', text)) < 2:
        return text

    original = text

    # Inner segments: "→ Xn + REAGENT → " ⇒ "→ Xn →(+REAGENT) ".
    # The arrow before Xn may already carry its own reagent parens
    # (from a previous rewrite step) — keep them.
    inner = re.compile(
        rf'→(\([^)]*\))?\s*({_UNKNOWN_NODE})\s*\+\s*([^→()]+?)\s*→'
    )
    prev = None
    while prev != text:
        prev = text
        text = inner.sub(
            lambda m: f"→{m.group(1) or ''} {m.group(2)} →(+{m.group(3).strip()}) ",
            text, count=1,
        )

    # First segment: "START + REAGENT → " ⇒ "START →(REAGENT) "
    # (single '+' only; START must not itself be an unknown node)
    first = re.match(
        rf'^\s*(?!(?:{_UNKNOWN_NODE})\b)([A-Z][A-Za-z0-9₀-₉()]*)\s*\+\s*([^→+()]+?)\s*→',
        text,
    )
    if first:
        text = (
            f"{first.group(1)} →({first.group(2).strip()}) "
            + text[first.end():].lstrip()
        )

    text = re.sub(r'  +', ' ', text).strip()
    if text != original.strip():
        # ascii(): arrows/subscripts crash structlog on cp1251 Windows
        # consoles, and the raised UnicodeEncodeError would kill extraction.
        logger.info(
            "chain_canonicalized",
            before=ascii(original[:60]), after=ascii(text[:60]),
        )
    return text


def _strip_inline_options(q: dict) -> None:
    """
    ISSUE 4(d): options extracted AND still sitting inside the stem as an
    inline "A) ... B) ... C) ..." block → truncate the stem at the block.
    Safety: only when >= 2 of the extracted option texts actually appear in
    the removed tail, so a stem legitimately discussing "A)" is untouched.
    """
    opts = {k: v for k, v in (q.get("options") or {}).items() if v}
    if len(opts) < 3:
        return
    stem = q.get("question_text") or ""
    m = re.search(r'\bA\)\s*', stem)
    if not m:
        return
    tail = stem[m.start():]
    if "B)" not in tail or "C)" not in tail:
        return
    tail_norm = _fp_norm(tail)
    hits = sum(
        1 for v in opts.values()
        if _fp_norm(str(v))[:12] and _fp_norm(str(v))[:12] in tail_norm
    )
    if hits >= 2:
        q["question_text"] = stem[:m.start()].rstrip()
        logger.info("inline_options_stripped", question=q.get("question_number"))


# ── Unanswerable-question detection (ISSUE 2) ────────────────────────────────

_ASK_VERBS = ("toping", "aniqlang", "hisoblang", "qaysi")
_UNKNOWN_TOKEN_RE = re.compile(r'\b([XYZ]|[ABD])([₀-₉0-9])?\b')


def find_unanswerable(questions: list[dict]) -> list[tuple[int, int, list[str]]]:
    """
    A question that ASKS about an unknown (X, A, B, D...) is unanswerable if
    that symbol never appears in any of its given reactions/chains — a
    reaction was lost at extraction. Only judged when the stem contains
    reaction syntax at all ("A vitamini ..." in a biology test never trips).
    Returns (section, number, missing_symbols).
    """
    out: list[tuple[int, int, list[str]]] = []
    for q in questions:
        stem = q.get("question_text") or ""
        if "→" not in stem and "=" not in stem:
            continue
        parts = re.split(r'[.?!;\n]', stem)
        ask = next(
            (p for p in reversed(parts)
             if any(v in p.lower() for v in _ASK_VERBS)),
            None,
        )
        if not ask:
            continue
        reactions = " ".join(
            p for p in parts if ("→" in p or "=" in p) and p is not ask
        )
        if not reactions:
            continue
        missing = sorted({
            m.group(0) for m in _UNKNOWN_TOKEN_RE.finditer(ask)
            if not re.search(rf'\b{re.escape(m.group(0))}\b', reactions)
            # FALSE-POSITIVE GUARD: a symbol the stem itself DEFINES is a given,
            # not a lost unknown — e.g. a set/variable "A = {1;2;3}", "B = {x|..}"
            # (a set-theory question) or "y = f(x)". A lost chemistry unknown
            # ("X ni toping") has no such "X =" definition, so it still trips.
            and not re.search(rf'\b{re.escape(m.group(0))}\b\s*[=:∈]', stem)
        })
        if missing:
            out.append((q.get("section", 1), q.get("question_number", 0), missing))
            logger.warning(
                "unanswerable_question",
                question=q.get("question_number"), missing=missing,
            )
    return out


# ── Export-time lint (ISSUE 6) ───────────────────────────────────────────────

def export_lint(questions: list[dict]) -> list[tuple[int, str]]:
    """
    Content-shape checks run right before variant generation. Returns
    (question_number, violation) pairs — surfaced to the teacher and logged,
    so a regression can never ship silently.
    """
    violations: list[tuple[int, str]] = []
    for q in questions:
        n = q.get("question_number", 0)
        stem = q.get("question_text") or ""

        if re.match(r'\s*\d+\s*[.)]\s', stem):
            violations.append((n, "stem_starts_with_number"))

        desc = q.get("image_description") or ""
        if desc:
            dt = set(re.findall(r'[^\W_]{3,}', desc.lower()))
            st = set(re.findall(r'[^\W_]{3,}', stem.lower()))
            if dt and len(dt & st) / len(dt) >= 0.7:
                violations.append((n, "desc_echoes_stem"))

        opts = {k: v for k, v in (q.get("options") or {}).items() if v}
        if len(opts) >= 4 and re.search(r'\bA\)', stem) and re.search(r'\bB\)', stem):
            violations.append((n, "options_inside_stem"))

    for _sec, n, missing in find_unanswerable(questions):
        violations.append((n, "unanswerable:" + ",".join(missing)))

    if violations:
        logger.warning("export_lint_violations", violations=violations)
    return violations


def _is_open_ended(q: dict) -> bool:
    """
    BUG FIX: Detect questions that genuinely have no answer options.
    These are open-ended questions (like 'n ∈ N, 7n+4 juft bo'lsa...').
    They should be kept but marked clearly so the PDF generator can
    render them without A/B/C/D slots.
    """
    opts = q.get("options", {})
    filled = [v for v in opts.values() if v and v.strip()]
    return len(filled) == 0


def _white_ratio(img: Image.Image) -> float:
    """Fraction of near-white pixels (0..1). On any error returns 0 so a page
    is NEVER skipped by mistake."""
    try:
        hist = img.convert("L").histogram()
        total = sum(hist) or 1
        return sum(hist[248:256]) / total
    except Exception:
        return 0.0


class AIAnalyzer:
    def __init__(self) -> None:
        self.model = genai.GenerativeModel(settings.GEMINI_MODEL)
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        # cost cache: md5(page image bytes) → extracted questions (this session)
        self._page_cache: dict[str, list[dict]] = {}

    # ── Gemini call ─────────────────────────────────────────────────────────────

    def _call_sync_multi(self, prompt: str, images_bytes: list[bytes]) -> tuple[str, int]:
        """Call Gemini with one or more PNG images.
        Returns (text, finish_reason) — finish_reason 2 means MAX_TOKENS,
        i.e. the output was truncated and the caller should paginate."""
        parts: list = [prompt]
        parts += [{"mime_type": "image/png", "data": b} for b in images_bytes]
        response = self.model.generate_content(
            parts,
            generation_config=genai.GenerationConfig(
                temperature=0.1,
                # Dense (two-column) pages can exceed any fixed cap — callers
                # must check finish_reason and paginate; the salvage parser in
                # _parse is only the last-resort safety net.
                max_output_tokens=8192,
                response_mime_type="application/json",
            ),
        )
        # Instrumentation only — read-only usage accounting, never crashes.
        try:
            from app.services.usage_log import log_gemini_usage
            log_gemini_usage(response, kind="extract", model=settings.GEMINI_MODEL)
        except Exception:
            pass
        fr = 0
        if response.candidates:
            fr = int(response.candidates[0].finish_reason)
            if fr == 4:
                raise RuntimeError("RECITATION_BLOCK page blocked by Gemini copyright filter")
            if fr == 2:  # MAX_TOKENS — output truncated
                logger.warning("gemini_output_truncated", finish_reason=fr)
            elif fr not in (0, 1):  # anything but UNSPECIFIED/STOP
                logger.warning("gemini_finish_reason", finish_reason=fr)
        return response.text, fr

    def _call_sync(self, prompt: str, image_bytes: bytes) -> tuple[str, int]:
        """Call Gemini with one PNG image."""
        return self._call_sync_multi(prompt, [image_bytes])

    async def _call(self, prompt: str, image_bytes: bytes) -> tuple[str, int]:
        async with self._sem:
            return await asyncio.wait_for(
                asyncio.to_thread(self._call_sync, prompt, image_bytes),
                timeout=CALL_TIMEOUT,
            )

    async def _call_multi(self, prompt: str, images: list[Image.Image]) -> tuple[str, int]:
        blobs: list[bytes] = []
        for img in images:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            blobs.append(buf.getvalue())
        async with self._sem:
            return await asyncio.wait_for(
                asyncio.to_thread(self._call_sync_multi, prompt, blobs),
                timeout=CALL_TIMEOUT,
            )

    # ── Per-page extraction ─────────────────────────────────────────────────────

    async def _extract_page(
        self, page_num: int, img: Image.Image, info: dict | None = None,
    ) -> list[dict]:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        img_bytes = buf.getvalue()

        # ── Cost cache: an identical page (same bytes) was already read ───────
        key = hashlib.md5(img_bytes).hexdigest()
        if key in self._page_cache:
            logger.info("page_cache_hit", page=page_num)
            dup = copy.deepcopy(self._page_cache[key])
            for q in dup:
                q["page_number"] = page_num
            return dup

        # ── Cost skip: a blank or header-only page with NO figure ────────────
        # SAFETY: never skip a page that carries a figure/table (has_visual) —
        # a text-light figure page (e.g. the number line) must still be read.
        if info is not None and not info.get("has_visual"):
            if info.get("text_len", 999) < 100 or _white_ratio(img) > 0.90:
                logger.info(
                    "page_skipped", page=page_num,
                    text_len=info.get("text_len"),
                )
                self._page_cache[key] = []
                return []

        for attempt in range(settings.GEMINI_MAX_RETRIES):
            try:
                raw, fr = await self._call(VISION_PROMPT, img_bytes)
                questions = self._parse(raw, page_num)

                # ── Continuation pagination ──────────────────────────────────
                # BUG FIX: dense pages exceed the output-token cap; previously
                # the truncated tail was simply lost (salvage kept only the
                # head), which dropped whole blocks of questions, section
                # markers and options. Now we keep asking the SAME page for
                # the rest until it completes (bounded rounds).
                rounds = 0
                try:
                    while fr == 2 and questions and rounds < MAX_CONTINUATIONS:
                        rounds += 1
                        last_n = next(
                            (x.get("question_number")
                             for x in reversed(questions)
                             if x.get("question_number")),
                            0,
                        )
                        raw, fr = await self._call(
                            VISION_PROMPT + CONTINUATION_NOTE.format(last=last_n),
                            img_bytes,
                        )
                        more = self._parse(raw, page_num)
                        known = {
                            x.get("question_number") for x in questions
                            if x.get("question_number")
                        }
                        new_items = [
                            m for m in more
                            if not m.get("question_number")
                            or m["question_number"] not in known
                        ]
                        if not new_items:
                            break
                        questions.extend(new_items)
                        logger.info(
                            "page_continuation",
                            page=page_num, round=rounds, added=len(new_items),
                        )
                except Exception as e:
                    # Keep what we have — a failed continuation must not
                    # discard the questions already extracted.
                    logger.warning(
                        "page_continuation_failed", page=page_num, error=str(e)
                    )

                for q in questions:
                    q["page_number"] = page_num
                    clean_question(q)
                if questions:
                    logger.info("page_ok", page=page_num, found=len(questions))
                else:
                    logger.warning("page_empty", page=page_num, raw_preview=raw[:300])
                # cache the successful result for duplicate pages this session
                self._page_cache[key] = copy.deepcopy(questions)
                return questions
            except asyncio.TimeoutError:
                logger.warning("page_timeout", page=page_num, attempt=attempt + 1)
            except Exception as e:
                logger.warning("page_error", page=page_num, attempt=attempt + 1, error=str(e))
            if attempt < settings.GEMINI_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        return []

    # ── Public ──────────────────────────────────────────────────────────────────

    async def extract_all_questions(
        self,
        images: list[Image.Image],
        page_infos: list[dict] | None = None,
        **_kw,
    ) -> list[dict[str, Any]]:
        if not images:
            return []

        # BUG FIX: Run pages in order, preserving page_num sequence.
        # asyncio.gather preserves result ORDER (index matches task index),
        # so results[0] = page 1, results[1] = page 2, etc.
        # We use return_exceptions=True to not crash on one bad page.
        tasks = [
            self._extract_page(
                i + 1, img,
                page_infos[i] if page_infos and i < len(page_infos) else None,
            )
            for i, img in enumerate(images)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # BUG FIX: Collect questions PAGE BY PAGE in order.
        # This ensures that when we merge a question split across pages,
        # we always see the earlier page's version first.
        all_q_by_page: list[list[dict]] = []
        for page_idx, r in enumerate(results):
            page_num = page_idx + 1
            if isinstance(r, Exception):
                logger.error("page_task_failed", page=page_num, error=str(r))
                all_q_by_page.append([])
            else:
                all_q_by_page.append(r)

        unique = self._merge_pages(all_q_by_page)
        # Order matters: recover whole missing questions FIRST, then run the
        # options pass — so a question recovered stem-only gets a targeted
        # options fetch instead of silently becoming open-ended.
        unique = await self._recover_missing_questions(unique, images)
        unique = await self._recover_missing_options(unique, images)

        # ── Post-extraction stem cleaning (ISSUES 1, 4b, 4d) ─────────────────
        for q in unique:
            _strip_own_number(q)
            _strip_leading_backticks(q)
            _strip_inline_options(q)
            q["question_text"] = canonicalize_chain_text(q.get("question_text"))

        unique.sort(key=lambda x: (x.get("section", 1), x.get("question_number", 0)))
        logger.info("extraction_done", total=len(unique), pages=len(images))
        return unique

    # ── Scheme recovery ladder (FIX 2 + FIX 3) ──────────────────────────────────

    async def ensure_scheme_content(
        self,
        questions: list[dict[str, Any]],
        images: list[Image.Image],
        pdf_bytes: bytes | None = None,
        col_map: dict[int, dict] | None = None,
        src_pages: list | None = None,
    ) -> list[tuple[int, int]]:
        """
        Every question that NEEDS scheme content (image flagged, description
        present, or trigger phrase in the stem) must actually HAVE it. Ladder:
        (a) geometric re-crop of the stem→options region (garbage-checked);
        (b) Gemini transcription into a text chain (accepted only with "→"
            and a formula);
        (c) formula-bearing description from the same call;
        (d) useless descriptions ("cut off" etc.) are nulled — never printed.
        Returns (section, question_number) pairs still lacking content
        (teacher warning).
        """
        failed: list[tuple[int, int]] = []
        src_lookup = {p.page_number: p for p in (src_pages or [])}
        pdf_sizes: dict[int, tuple[float, float]] = {}
        if pdf_bytes:
            try:
                import fitz
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                for i, page in enumerate(doc):
                    pdf_sizes[i + 1] = (page.rect.width, page.rect.height)
                doc.close()
            except Exception as e:
                logger.warning("scheme_pdf_sizes_failed", error=str(e))

        for q in questions:
            # ISSUE 4(a): a description that merely restates a chain already
            # present in the stem is dropped before any other decision.
            if q.get("image_description") and _desc_redundant(
                q.get("question_text"), q["image_description"]
            ):
                logger.info(
                    "redundant_desc_dropped", question=q.get("question_number")
                )
                q["image_description"] = None

            if not _needs_scheme(q) or _has_scheme_content(q):
                continue
            n = q.get("question_number", 0)
            page = q.get("page_number") or 0
            mapping = (col_map or {}).get(page)

            # (a) geometric re-crop within the question's own column band
            if pdf_bytes and mapping and mapping["src_page"] in src_lookup \
                    and mapping["src_page"] in pdf_sizes:
                from app.services.file_processor import recrop_scheme_region
                pdf_w, pdf_h = pdf_sizes[mapping["src_page"]]
                try:
                    path = recrop_scheme_region(
                        pdf_bytes=pdf_bytes,
                        src_page=mapping["src_page"],
                        src_image=src_lookup[mapping["src_page"]].image,
                        page_pdf_size=(pdf_w, pdf_h),
                        x_range=(mapping["x0"] * pdf_w, mapping["x1"] * pdf_w),
                        q_num=n,
                        analysis_page=page,
                        all_questions=questions,
                        stem_text=q.get("question_text"),
                    )
                except Exception as e:
                    logger.warning("scheme_recrop_failed", question=n, error=str(e))
                    path = None
                if path:
                    q["image_path"] = path
                    q["has_image"] = True
                    logger.info("scheme_recovered_by_recrop", question=n)
                    continue

            # (b)/(c) Gemini transcription of the scheme
            if 1 <= page <= len(images):
                try:
                    raw, _fr = await self._call_multi(
                        TRANSCRIBE_SCHEME_PROMPT.format(n=n), [images[page - 1]]
                    )
                    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
                    data = self._try_json(cleaned) or {}
                except Exception as e:
                    logger.warning("scheme_transcribe_failed", question=n, error=str(e))
                    data = {}
                chain = clean_latex(str(data.get("chain") or "")).strip()
                # Accept a chain (has arrows) OR verbatim TABLE rows
                # ("1. <cell>; <cell>" lines) — never prose (ISSUE 4c).
                is_chain = "→" in chain
                is_table = (
                    len(re.findall(r'^\d+\.\s', chain, re.M)) >= 2
                )
                if (is_chain or is_table) and FORMULA_RE.search(chain):
                    if is_chain:
                        chain = canonicalize_chain_text(chain)
                    q["question_text"] = (
                        str(q.get("question_text") or "").rstrip() + "\n" + chain
                    )
                    q["has_image"] = False
                    q["image_description"] = None
                    logger.info(
                        "scheme_recovered_by_transcription",
                        question=n, kind="table" if is_table else "chain",
                    )
                    continue
                desc = clean_latex(str(data.get("desc") or "")).strip()
                if _is_meta_desc(desc):
                    # Gemini answered "this has no scheme" in prose — not a figure
                    # description. Never store it (it would print as a [Rasm] box).
                    logger.info("scheme_meta_desc_rejected", question=n)
                elif desc and FORMULA_RE.search(desc) and not _USELESS_DESC_RE.search(desc):
                    q["image_description"] = desc
                    logger.info("scheme_recovered_by_description", question=n)
                    continue

            # (d) unrecoverable: drop ONLY a truly contentless description
            # ("cut off", "not readable"). BUG 2: previously we also nulled
            # any description lacking a CHEMICAL formula, which deleted the
            # perfectly good text of non-chemistry figures — number lines,
            # geometry diagrams, graphs — leaving has_image=True with nothing
            # to render (a blank, unsolvable question). A content-bearing
            # description is the designed fallback; keep it so the PDF shows
            # the [Rasm] box instead of empty space.
            if q.get("image_description") and _USELESS_DESC_RE.search(
                q["image_description"]
            ):
                q["image_description"] = None
            failed.append((q.get("section", 1), n))
            logger.warning("scheme_unrecoverable", question=n)

        return failed

    # ── Strict re-extraction of flagged questions (FIX 7) ───────────────────────

    async def reextract_questions(
        self,
        numbers: list[int],
        questions: list[dict[str, Any]],
        images: list[Image.Image],
    ) -> dict[int, dict]:
        """
        Re-extract specific (suspicious) questions with strict symbol
        preservation. Uses the existing targeted-recovery machinery.
        Returns {number: fresh_question_dict} for successfully re-read ones —
        the caller decides how to apply (DB update); nothing is auto-rewritten
        here.
        """
        by_num = {q.get("question_number"): q for q in questions}
        by_page: dict[int, list[int]] = {}
        for n in numbers:
            page = (by_num.get(n) or {}).get("page_number") or 0
            if 1 <= page <= len(images):
                by_page.setdefault(page, []).append(n)
            else:
                logger.warning("reextract_no_page", question=n)

        fresh: dict[int, dict] = {}
        for page, nums in sorted(by_page.items()):
            for i in range(0, len(nums), 8):
                chunk = nums[i:i + 8]
                prompt = RECOVER_QUESTIONS_PROMPT.format(
                    nums=", ".join(str(n) for n in chunk)
                ) + REEXTRACT_STRICT_NOTE
                imgs = [images[page - 1]]
                if page < len(images):
                    imgs.append(images[page])
                try:
                    raw, _fr = await self._call_multi(prompt, imgs)
                    items = self._parse(raw, page_num=page)
                except Exception as e:
                    logger.warning("reextract_failed", page=page, error=str(e))
                    continue
                for item in items:
                    clean_question(item)
                    n = item.get("question_number")
                    opts = {
                        k: v for k, v in (item.get("options") or {}).items()
                        if v and str(v).strip()
                    }
                    if (n in chunk
                            and str(item.get("question_text") or "").strip()
                            and len(opts) != 1):
                        item["options"] = opts
                        _strip_own_number(item)
                        _strip_inline_options(item)
                        item["question_text"] = canonicalize_chain_text(
                            item.get("question_text")
                        )
                        fresh[n] = item
                        logger.info("question_reextracted", question=n)
        return fresh

    # ── Missing-question recovery pass (numbering gaps) ─────────────────────────

    async def _recover_missing_questions(
        self,
        questions: list[dict[str, Any]],
        images: list[Image.Image],
        excluded: set[tuple[int, int]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Recover question numbers that are ENTIRELY absent after extraction
        (e.g. a consecutive block of dense reaction-chain questions Gemini
        skipped, or questions lost to output truncation). The gap's page is
        inferred from its nearest extracted neighbors (a missing question was
        never seen, so it carries no page itself). One targeted call per
        page-window; best-effort — failures keep today's behavior, and the
        #16 gap warning still reports anything left unrecovered.

        excluded: {(section, number)} that must NEVER be re-requested — the
        dedup removal registry. NOTE the pipeline-order contract: this pass
        runs at extraction time, BEFORE any teacher-driven duplicate
        resolution, so an excluded number cannot re-enter within one upload;
        the parameter is a guard for any future reordering.
        """
        excluded = excluded or set()
        by_sec: dict[int, list[dict]] = {}
        for q in questions:
            if q.get("question_number"):
                by_sec.setdefault(q.get("section", 1), []).append(q)

        for sec, qs in sorted(by_sec.items()):
            nums = sorted(q["question_number"] for q in qs)
            present = set(nums)
            max_n = nums[-1]
            gaps = [
                x for x in range(1, max_n + 1)
                if x not in present and (sec, x) not in excluded
            ]
            if not gaps:
                continue

            page_of = {
                q["question_number"]: q.get("page_number") or 0 for q in qs
            }

            # Group gap numbers by the page window of their neighbors.
            windows: dict[tuple[int, int], list[int]] = {}
            for g in gaps:
                lo = max((n for n in nums if n < g), default=None)
                hi = min((n for n in nums if n > g), default=None)
                p_lo = page_of.get(lo) or page_of.get(hi) or 0
                p_hi = page_of.get(hi) or p_lo
                if not (1 <= p_lo <= len(images)):
                    continue
                p_hi = min(max(p_hi, p_lo), len(images))
                # Cap the window at 2 pages (neighbors of a consecutive
                # block are almost always on the same or adjacent pages).
                p_hi = min(p_hi, p_lo + 1)
                windows.setdefault((p_lo, p_hi), []).append(g)

            for (p1, p2), gnums in sorted(windows.items()):
                # Chunk the request so the recovery response can never hit
                # the output-token cap itself (self-truncation would insert
                # stem-only questions — the 47-open-ended failure mode).
                for i in range(0, len(gnums), 8):
                    chunk = gnums[i:i + 8]
                    prompt = RECOVER_QUESTIONS_PROMPT.format(
                        nums=", ".join(str(n) for n in chunk)
                    )
                    imgs = [images[p1 - 1]]
                    if p2 != p1:
                        imgs.append(images[p2 - 1])
                    try:
                        raw, _fr = await self._call_multi(prompt, imgs)
                        items = self._parse(raw, page_num=p1)
                    except Exception as e:
                        logger.warning(
                            "question_recovery_failed",
                            section=sec, pages=(p1, p2), error=str(e),
                        )
                        continue
                    for item in items:
                        clean_question(item)
                    inserted = self._apply_recovered_questions(
                        questions, items, set(chunk), sec, p1
                    )
                    logger.info(
                        "question_recovery_pass",
                        section=sec, pages=(p1, p2),
                        asked=chunk, recovered=inserted,
                    )
        return questions

    @staticmethod
    def _apply_recovered_questions(
        questions: list[dict],
        items: list[dict],
        expected: set[int],
        section: int,
        default_page: int,
    ) -> int:
        """
        Insert recovered whole questions. Hallucination guards:
        - only numbers we explicitly asked for are accepted
        - never overwrites an existing (section, number)
        - empty question text rejected
        - exactly 1 option rejected (broken, not gradeable);
          0 options accepted as open-ended
        Returns how many questions were inserted.
        """
        existing = {
            (q.get("section", 1), q.get("question_number")) for q in questions
        }
        inserted = 0
        for item in items:
            n = item.get("question_number")
            if n not in expected or (section, n) in existing:
                continue
            if not str(item.get("question_text") or "").strip():
                continue
            opts = {
                k: v for k, v in (item.get("options") or {}).items()
                if v and str(v).strip()
            }
            if len(opts) == 1:
                logger.info("gap_recovery_single_option_rejected", question=n)
                continue
            item["options"] = opts
            item["is_open_ended"] = len(opts) == 0
            item["section"] = section
            if not item.get("page_number"):
                item["page_number"] = default_page
            questions.append(item)
            existing.add((section, n))
            inserted += 1
            logger.info(
                "question_recovered",
                question=n, section=section, options=len(opts),
            )
        return inserted

    # ── Zero-option recovery pass (#1) ──────────────────────────────────────────

    async def _recover_missing_options(
        self, questions: list[dict[str, Any]], images: list[Image.Image]
    ) -> list[dict[str, Any]]:
        """
        BUG FIX (#1): a question that ends up with zero options is not always
        open-ended — its options may simply have been lost during extraction.
        Before declaring it open-ended, make ONE targeted Gemini call per
        affected page asking specifically for those questions' options,
        sending the page plus the following page (options may sit across
        the break). Best-effort: any failure leaves today's behavior.
        """
        by_page: dict[int, list[dict]] = {}
        for q in questions:
            # A figure-based WRITE-IN question is legitimately option-less — it is
            # NOT "options lost during extraction". Sending it to options recovery
            # wastes a Gemini call and, worse, risks Gemini hallucinating options
            # off the diagram, converting a real open figure-question into a bogus
            # MC one (which then grades against a fabricated key). Skip it.
            if q.get("has_image") or q.get("image_description"):
                continue
            if q.get("is_open_ended") and not q.get("options"):
                page = q.get("page_number") or 0
                if 1 <= page <= len(images):
                    by_page.setdefault(page, []).append(q)

        if not by_page:
            return questions

        for page, page_targets in sorted(by_page.items()):
            nums = [q.get("question_number") for q in page_targets]
            prompt = RECOVER_OPTIONS_PROMPT.format(
                nums=", ".join(str(n) for n in nums)
            )
            imgs = [images[page - 1]]
            if page < len(images):
                imgs.append(images[page])
            try:
                raw, _fr = await self._call_multi(prompt, imgs)
                items = self._parse(raw, page_num=page)
            except Exception as e:
                logger.warning("options_recovery_failed", page=page, error=str(e))
                continue
            for item in items:
                clean_question(item)
            repaired = self._apply_recovered_options(page_targets, items)
            logger.info(
                "options_recovery_pass",
                page=page,
                asked=nums,
                repaired=repaired,
            )
        return questions

    @staticmethod
    def _apply_recovered_options(
        targets: list[dict], recovered: list[dict]
    ) -> int:
        """
        Apply a recovery-pass result to zero-option questions.
        Hallucination guard: a result is accepted only if it carries at
        least 2 non-empty options for that question number — otherwise the
        question stays open-ended. Returns how many questions were repaired.
        """
        by_num = {q.get("question_number"): q for q in targets}
        repaired = 0
        for item in recovered:
            target = by_num.get(item.get("question_number"))
            if target is None:
                continue
            opts = {
                k: v
                for k, v in (item.get("options") or {}).items()
                if v and str(v).strip()
            }
            if len(opts) < 2:
                logger.info(
                    "recovery_confirmed_open_ended",
                    question=item.get("question_number"),
                )
                continue
            target["options"] = opts
            target["is_open_ended"] = False
            repaired += 1
            logger.info(
                "options_recovered",
                question=item.get("question_number"),
                letters=sorted(opts),
            )
        return repaired

    # ── Cross-page merge & stitching ────────────────────────────────────────────

    @staticmethod
    def _merge_pages(all_q_by_page: list[list[dict]]) -> list[dict[str, Any]]:
        """
        Merge per-page extractions into one ordered question list.

        Steps:
        1. BUG FIX (#6): stitch page-leading fragments (an options block or
           text continuation with no question number) onto the last numbered
           question of the IMMEDIATELY preceding page. Previously these
           orphans were silently dropped, so a question whose options landed
           on the next page lost them and was misrendered as open-ended.
           Only the directly preceding page is used as the stitch target —
           if that page yielded no numbered questions (failed/blocked), the
           fragment is dropped with a warning rather than risk stitching it
           onto the wrong question.
        2. Assign section indexes: a document may contain several independent
           tests whose numbering restarts at 1. A new section starts when
           Gemini reports a section title, or when the question number drops
           back to <= 3 (restart rule; the <= 3 tolerance prevents phantom
           sections if pages are ever extracted slightly out of order).
        3. Merge duplicate question numbers across pages (fill missing
           options) — keyed by (section, number) so "question 1" of test 1
           never collides with "question 1" of test 2.
        4. Mark genuinely open-ended questions (no options anywhere).
        """
        prev_last: dict | None = None  # last numbered question of the previous page
        pages_clean: list[list[dict]] = []
        current_section = 1
        last_num: int | None = None

        for page_idx, page_questions in enumerate(all_q_by_page):
            page_num = page_idx + 1
            rest = list(page_questions)

            # ── Stitch leading fragments onto the question cut by the break ──
            while rest and rest[0].get("is_fragment"):
                frag = rest.pop(0)
                if prev_last is None:
                    logger.warning("stitch_orphan_dropped", page=page_num)
                    continue

                # Options may be keyed "opts" (new, real labels) or "options"
                # (legacy). Stitch by the REAL label, any script.
                opts = prev_last.get("opts")
                if not isinstance(opts, dict):
                    opts = prev_last.get("options")
                    if not isinstance(opts, dict):
                        opts = {}
                    prev_last["opts"] = opts
                complete_before = sum(
                    1 for v in opts.values() if v and str(v).strip()
                ) >= 4

                frag_opts = frag.get("opts") or frag.get("options") or {}
                filled: list[str] = []
                for letter, val in frag_opts.items():
                    if not opts.get(letter) and val:
                        opts[letter] = val
                        filled.append(letter)

                frag_text = str(frag.get("question_text") or "").strip()
                appended = False
                if frag_text:
                    if filled or not complete_before:
                        prev_last["question_text"] = (
                            str(prev_last.get("question_text") or "").rstrip()
                            + " " + frag_text
                        )
                        appended = True
                    else:
                        # Target already complete and the fragment brings no
                        # options — more likely an unnumbered NEW question.
                        # Don't corrupt the previous one.
                        logger.warning(
                            "stitch_text_dropped",
                            page=page_num,
                            target=prev_last.get("question_number"),
                        )

                if filled or appended:
                    logger.info(
                        "stitched_fragment",
                        page=page_num,
                        target=prev_last.get("question_number"),
                        letters=filled,
                        text_appended=appended,
                    )
                else:
                    logger.warning(
                        "stitch_noop",
                        page=page_num,
                        target=prev_last.get("question_number"),
                    )

            pages_clean.append(rest)

            # ── Section assignment (reading order) ──────────────────────────
            inversions = 0
            for q in rest:
                n = q.get("question_number")
                if not n:
                    continue
                if last_num is not None and n < last_num:
                    # A genuine restart is a LARGE drop back to the start
                    # (e.g. 52 -> 1). A small decrease (2 after 3) is far more
                    # likely extraction noise, so require either Gemini's
                    # section-title signal or a drop of >= 5 down to <= 3.
                    if q.get("section_title") or (n <= 3 and last_num - n >= 5):
                        current_section += 1
                        logger.info(
                            "section_start",
                            section=current_section,
                            page=page_num,
                            first_question=n,
                            title=q.get("section_title"),
                        )
                    else:
                        # Decrease without a restart signature — suspicious
                        # ordering (possible two-column interleave).
                        inversions += 1
                q["section"] = current_section
                last_num = n
            if inversions >= 3:
                logger.warning(
                    "possible_column_interleave",
                    page=page_num,
                    inversions=inversions,
                )

            # The stitch target for the NEXT page is this page's last
            # numbered question (reading order = the one a break would cut).
            page_last: dict | None = None
            for q in rest:
                if q.get("question_number"):
                    page_last = q
            prev_last = page_last

        # ── Merge: questions split across pages ─────────────────────────────
        # Strategy:
        # 1. First pass — collect all occurrences of each question number,
        #    in page order.
        # 2. For each question number, start from the first occurrence and
        #    fill in missing options from subsequent pages (handles 3-page splits).
        # 3. Mark open-ended questions (no options anywhere) clearly.

        # {(section, question_number): [q_dict from page1, q_dict from page2, ...]}
        occurrences: dict[tuple[int, int], list[dict]] = {}
        for page_questions in pages_clean:
            for q in page_questions:
                n = q.get("question_number", 0)
                if not n:
                    continue
                occurrences.setdefault((q.get("section", 1), n), []).append(q)

        merged: dict[tuple[int, int], dict] = {}
        for (sec, n), qs in occurrences.items():
            # Start with the first occurrence (earliest page = most complete text)
            primary = qs[0]

            # BUG FIX: Merge options from ALL subsequent occurrences, not just one.
            # This handles questions whose options span pages 2 AND 3. Label-
            # agnostic: merges by the REAL printed label (any script), reading the
            # new "opts" key (with legacy "options" fallback).
            def _raw_opts(item):
                o = item.get("opts")
                if not isinstance(o, dict):
                    o = item.get("options") if isinstance(item.get("options"), dict) else {}
                return o

            for subsequent in qs[1:]:
                prim_opts = primary.get("opts")
                if not isinstance(prim_opts, dict):
                    prim_opts = _raw_opts(primary)
                    primary["opts"] = prim_opts
                for letter, new_val in _raw_opts(subsequent).items():
                    if not prim_opts.get(letter) and new_val:
                        prim_opts[letter] = new_val
                        logger.info(
                            "merged_option",
                            question=n,
                            letter=letter,
                            from_page=subsequent.get("page_number"),
                        )

                # Also merge image info if primary lacks it
                if not primary.get("has_image") and subsequent.get("has_image"):
                    primary["has_image"] = True
                    primary["image_description"] = subsequent.get("image_description")

            # BUG FIX: Detect and mark open-ended questions instead of
            # leaving them with no options and confusing the PDF generator.
            if _is_open_ended(primary):
                primary["is_open_ended"] = True
                logger.info(
                    "open_ended_question",
                    question=n,
                    text_preview=primary.get("question_text", "")[:60],
                )
            else:
                primary["is_open_ended"] = False

            merged[(sec, n)] = primary

        # Sort by (section, question number) so sections never interleave
        return sorted(
            merged.values(),
            key=lambda x: (x.get("section", 1), x.get("question_number", 0)),
        )

    async def analyze_answer_sheet(
        self, image: Image.Image, total_questions: int
    ) -> dict[str, str | None]:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        prompt = ANSWER_SHEET_PROMPT.format(total=total_questions)
        for attempt in range(settings.GEMINI_MAX_RETRIES):
            try:
                raw, _fr = await self._call(prompt, buf.getvalue())
                cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
                data = json.loads(cleaned)
                answers: dict[str, str | None] = {}
                for k, v in data.get("answers", {}).items():
                    answers[str(k)] = str(v).strip().upper()[:1] if v else None
                return answers
            except asyncio.TimeoutError:
                logger.warning("answer_sheet_timeout", attempt=attempt + 1)
            except Exception as e:
                logger.warning("answer_sheet_error", attempt=attempt + 1, error=str(e))
            if attempt < settings.GEMINI_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        return {}

    # ── Parser ──────────────────────────────────────────────────────────────────

    def _parse(self, raw: str, page_num: int = 0) -> list[dict[str, Any]]:
        """Extract JSON array from Gemini response robustly."""
        text = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

        data = self._try_json(text)

        if data is None:
            s = text.find("[")
            e = text.rfind("]")
            if s != -1 and e > s:
                data = self._try_json(text[s: e + 1])

        if data is None:
            for m in re.finditer(r'\[[\s\S]+?\]', text):
                data = self._try_json(m.group())
                if data is not None:
                    break

        # BUG FIX: salvage a TRUNCATED array (output hit the token limit and
        # the closing "]" never arrived). Cut back to the last complete
        # object and close the array — recovers every fully-emitted question
        # instead of losing the whole page.
        if data is None:
            s = text.find("[")
            e = text.rfind("}")
            if s != -1 and e > s:
                data = self._try_json(text[s:e + 1] + "]")
                if data is not None:
                    logger.warning(
                        "parse_salvaged_truncated",
                        page=page_num,
                        items=len(data) if isinstance(data, list) else 0,
                    )

        if data is None:
            logger.error("parse_failed", page=page_num, raw=raw[:500])
            return []

        if isinstance(data, dict):
            data = data.get("questions", data.get("groups", []))
            if isinstance(data, list) and data and isinstance(data[0], dict):
                if "questions" in data[0]:
                    flat = []
                    for g in data:
                        flat.extend(g.get("questions", []))
                    data = flat

        if not isinstance(data, list):
            logger.error("not_a_list", page=page_num, type=type(data).__name__)
            return []

        return self._normalize(data, page_num)

    @staticmethod
    def _try_json(text: str):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    def _normalize(self, questions: list, page_num: int = 0) -> list[dict[str, Any]]:
        """Convert various field-name styles to internal format."""
        result = []
        for q in questions:
            if not isinstance(q, dict):
                continue

            num = (
                q.get("question_number")
                or q.get("n")
                or q.get("number")
                or q.get("num")
                or 0
            )
            try:
                num = int(num)
            except (TypeError, ValueError):
                num = 0

            text = (
                q.get("question_text")
                or q.get("q")
                or q.get("text")
                or q.get("question")
                or ""
            )

            # Options keyed by their REAL printed label (Latin or Cyrillic), in
            # order. Accept the new "opts" key, then legacy shapes ("options" /
            # flat "A".."E") so old cached payloads still parse. Labels are NEVER
            # folded here — preservation is the whole point.
            opts_raw = q.get("opts")
            if not isinstance(opts_raw, dict):
                opts_raw = q.get("options") if isinstance(q.get("options"), dict) else {}
            options = {
                str(k).strip(): v for k, v in opts_raw.items()
                if str(k).strip() and v and str(v).strip()
            }
            if not options:                       # legacy flat A..E fallback
                for letter in "ABCDE":
                    if q.get(letter) and str(q[letter]).strip():
                        options[letter] = q[letter]

            # Correct-answer LABEL: keep it verbatim (single char, any script);
            # only accept it if it names one of this question's real options.
            ca = q.get("correct_answer") or q.get("ans") or q.get("answer")
            ca = str(ca).strip()[:1] if ca else None
            if ca not in options:
                ca = None

            has_img = bool(
                q.get("has_image_or_table")
                or q.get("has_image")
                or q.get("img")
            )
            img_desc = (
                q.get("visual_description")
                or q.get("image_description")
                or q.get("img_desc")
            )

            # Reading-comprehension / shared-passage grouping.
            # Namespace the label by page so the same label ("g1") appearing on
            # two different pages is never merged into one group. Truncate to fit
            # the Question.group_id String(64) column.
            grp = q.get("group") or q.get("group_id") or q.get("group_label")
            grp_ctx = (
                q.get("group_context")
                or q.get("context")
                or q.get("passage")
            )
            group_id = None
            group_context = None
            if grp not in (None, "", "null", "None"):
                group_id = f"p{page_num}_{str(grp).strip()}"[:64]
                group_context = clean_latex(str(grp_ctx)) if grp_ctx else None

            # BUG FIX (#6): an unnumbered item that carries options or text is
            # a page-break continuation of the previous page's last question.
            # Tag it so _merge_pages can stitch it instead of dropping it.
            is_fragment = num == 0 and (bool(options) or bool(str(text).strip()))

            # Multi-test documents: Gemini flags the first question of a new
            # section with its title. _merge_pages combines this with the
            # deterministic numbering-restart rule.
            sec_title = q.get("section") or q.get("section_title")
            sec_title = str(sec_title).strip() if sec_title and str(sec_title).strip() not in ("null", "None") else None

            result.append({
                "question_number": num,
                "question_text": str(text),
                "options": options,
                "correct_answer": ca,
                "has_image": has_img,
                "image_description": str(img_desc) if img_desc else None,
                "image_path": None,
                "is_open_ended": False,  # will be set in extract_all_questions
                "is_fragment": is_fragment,
                "verbatim_doubt": bool(q.get("verbatim_doubt")),
                "section_title": sec_title,
                "group_id": group_id,
                "group_context": group_context,
            })
        return result