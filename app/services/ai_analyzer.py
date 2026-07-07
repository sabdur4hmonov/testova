"""
AI question extractor — Gemini Vision, 1 page per call, parallel.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
from typing import Any

import google.generativeai as genai
from PIL import Image

from app.config import settings
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
    "A": "option A text exactly as written",
    "B": "option B text exactly as written",
    "C": "option C text exactly as written",
    "D": "option D text exactly as written",
    "ans": null,
    "img": false,
    "img_desc": null,
    "group": null,
    "group_context": null,
    "section": null
  }
]

CRITICAL RULES:
1. Extract ALL questions visible on this page, do not skip any
2. Keep the original question number (n) exactly as shown
3. Copy ALL text EXACTLY as written - do not change +/- signs, do not modify equations
4. For equations: copy character by character. If you see 3(x+1) write 3(x+1), NOT 3(x-1)
5. If a question has an image/diagram/table: set img=true, describe it in img_desc
6. For answer options: if options ARE on this page, copy them exactly
7. If answer options are NOT visible on this page (cut off), leave A/B/C/D as empty string ""
8. Do NOT invent or guess missing options - leave them as ""
9. Do NOT use LaTeX or $ symbols. Write math in plain text:
   - Fractions: write as (a)/(b) example: (1)/(2)
   - Powers: write as x^2 or x^n
   - Square root: write as sqrt(x)
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
16. RETURN ONLY THE JSON ARRAY - nothing else"""

ANSWER_SHEET_PROMPT = """Bu o'quvchining javob varaqasi. Test {total} ta savol.
Har savol uchun belgilangan javobni o'qi (A/B/C/D), bo'sh bo'lsa null.
FAQAT JSON: {{"answers": {{"1": "A", "2": null}}}}"""

# FIX 3(b): transcribe an unreadable/uncroppable scheme into a text chain.
TRANSCRIBE_SCHEME_PROMPT = """Question {n} on this test page contains a scheme/diagram of transformations
(compounds or states connected by arrows, possibly branching).

Transcribe that scheme as PLAIN TEXT:
- start item, then each step as →(reagent/condition)
- if the scheme branches into parallel paths, label them on separate lines:
  "Yuqori yo'l: ..." (top path) and "Pastki yo'l: ..." (bottom path)
- keep every formula/symbol EXACTLY as printed, as real Unicode

Return ONLY JSON: {{"chain": "...", "desc": "..."}}
- "chain": the transcribed text chain if you can read the scheme, else ""
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


def clean_question(q: dict) -> dict:
    """Apply clean_latex to all text fields of a question dict."""
    q["question_text"] = clean_latex(q.get("question_text", ""))
    opts = q.get("options", {})
    q["options"] = {k: clean_latex(v) for k, v in opts.items() if v}
    if q.get("image_description"):
        q["image_description"] = clean_latex(q["image_description"])
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


def _needs_scheme(q: dict) -> bool:
    """Does this question require visible scheme content to be answerable?"""
    if q.get("has_image") or q.get("image_description"):
        return True
    stem = (q.get("question_text") or "").lower()
    return any(p in stem for p in SCHEME_TRIGGER_PHRASES)


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

def _fp_norm(s: str | None) -> str:
    return re.sub(r'[^a-zа-яё0-9ʻʼ]', '', (s or '').lower())


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


def dedupe_questions(
    questions: list[dict],
) -> tuple[list[dict], list[tuple[int, int]], list[list[int]]]:
    """
    Remove EXACT duplicates only (identical fingerprint within a section):
    keep the first, report (kept_number, dropped_number) pairs.

    Near-matches — same stem but different options or scheme (legitimate
    sibling questions like KOH/NaOH variants) — are KEPT and reported as
    sibling groups for the teacher's information only.
    """
    kept: list[dict] = []
    seen_fp: dict[tuple[int, str], int] = {}    # (section, fingerprint) → number
    stems: dict[tuple[int, str], list[int]] = {}  # (section, stem) → numbers
    duplicates: list[tuple[int, int, int]] = []   # (section, kept_n, dropped_n)

    for q in questions:
        sec = q.get("section", 1)
        n = q.get("question_number", 0)
        fp = question_fingerprint(q)
        key = (sec, fp)
        if key in seen_fp:
            duplicates.append((sec, seen_fp[key], n))
            logger.warning(
                "duplicate_question_dropped",
                section=sec, kept=seen_fp[key], dropped=n,
            )
            continue
        seen_fp[key] = n
        stems.setdefault((sec, _fp_norm(q.get("question_text"))), []).append(n)
        kept.append(q)

    siblings = [
        (sec, nums) for (sec, stem), nums in stems.items()
        if stem and len(nums) > 1
    ]
    return kept, duplicates, siblings


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


class AIAnalyzer:
    def __init__(self) -> None:
        self.model = genai.GenerativeModel(settings.GEMINI_MODEL)
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)

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

    async def _extract_page(self, page_num: int, img: Image.Image) -> list[dict]:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        img_bytes = buf.getvalue()

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
        **_kw,
    ) -> list[dict[str, Any]]:
        if not images:
            return []

        # BUG FIX: Run pages in order, preserving page_num sequence.
        # asyncio.gather preserves result ORDER (index matches task index),
        # so results[0] = page 1, results[1] = page 2, etc.
        # We use return_exceptions=True to not crash on one bad page.
        tasks = [self._extract_page(i + 1, img) for i, img in enumerate(images)]
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
                if "→" in chain and FORMULA_RE.search(chain):
                    q["question_text"] = (
                        str(q.get("question_text") or "").rstrip() + "\n" + chain
                    )
                    q["has_image"] = False
                    q["image_description"] = None
                    logger.info("scheme_recovered_by_transcription", question=n)
                    continue
                desc = clean_latex(str(data.get("desc") or "")).strip()
                if desc and FORMULA_RE.search(desc) and not _USELESS_DESC_RE.search(desc):
                    q["image_description"] = desc
                    logger.info("scheme_recovered_by_description", question=n)
                    continue

            # (d) unrecoverable: never print a contentless description
            if q.get("image_description") and (
                _USELESS_DESC_RE.search(q["image_description"])
                or not FORMULA_RE.search(q["image_description"])
            ):
                q["image_description"] = None
            failed.append((q.get("section", 1), n))
            logger.warning("scheme_unrecoverable", question=n)

        return failed

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
        runs BEFORE dedupe_questions (extract → recover → dedupe), so a
        removed duplicate cannot re-enter within one upload; the parameter
        is a guard for any future reordering.
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

                opts = prev_last.setdefault("options", {})
                complete_before = sum(
                    1 for v in opts.values() if v and str(v).strip()
                ) >= 4

                frag_opts = frag.get("options", {})
                filled: list[str] = []
                for letter in "ABCDE":
                    if not opts.get(letter) and frag_opts.get(letter):
                        opts[letter] = frag_opts[letter]
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
            # This handles questions whose options span pages 2 AND 3.
            for subsequent in qs[1:]:
                sub_opts = subsequent.get("options", {})
                for letter in "ABCD":
                    existing_val = primary.get("options", {}).get(letter, "")
                    new_val = sub_opts.get(letter, "")
                    if not existing_val and new_val:
                        primary.setdefault("options", {})[letter] = new_val
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

            opts_raw = q.get("options") or {}
            if isinstance(opts_raw, dict):
                options = {k: v for k, v in opts_raw.items() if k in "ABCDE" and v}
            else:
                options = {}
            for letter in "ABCDE":
                if not options.get(letter) and q.get(letter):
                    options[letter] = q[letter]

            ca = q.get("correct_answer") or q.get("ans") or q.get("answer")
            ca = str(ca).strip().upper()[:1] if ca else None
            if ca not in ("A", "B", "C", "D", "E"):
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
                "section_title": sec_title,
                "group_id": group_id,
                "group_context": group_context,
            })
        return result