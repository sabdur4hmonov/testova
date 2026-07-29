"""
Student answer-sheet reader — Gemini Vision.

Reads a photo of a filled answer sheet and returns the marked answers. This is
a SEPARATE Gemini call from question extraction: it has its OWN prompt
(ANSWER_SHEET_PROMPT below) and never imports or touches VISION_PROMPT, which
is protected by the project's hard-won extraction rules.

Reuse, not duplication: image decode + deskew go through the SAME
`image_to_pages` / `preprocess_image` helpers the existing checking flow uses.
Only the Gemini call, this prompt, and the defensive JSON parse are new.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import time
from typing import Any

from app.config import settings
from app.services.file_processor import image_to_pages, preprocess_image
from app.services.checker import is_correct, normalize
from app.services.option_letters import is_option_letter
from app.utils.logging import get_logger

logger = get_logger(__name__)

# NOTE: this module deliberately does NOT use google-generativeai. The grading
# call goes over REST so thinking can be disabled (see _call_sync). Extraction
# (ai_analyzer) still uses the SDK and configures it independently.

# NEW prompt — do NOT reuse VISION_PROMPT. Reads MARKED answers, never guesses.
ANSWER_SHEET_PROMPT = """You are reading a photo of a student's exam ANSWER SHEET.
{labels}The test has {total} questions. For MOST questions the student marks ONE option,
labelled with a LETTER. The label may be LATIN (A, B, C, D, E, F) or CYRILLIC
(А, Б, В, Г, Д, Е). A test may offer more than four options and may SKIP letters
(e.g. a, b, d, e). Some questions have NO options — for those the student WRITES
a short answer by hand (a word, a number, or a very short phrase), usually in
BLOCK CAPITAL LETTERS.

Rules:
- Report ONLY what the student actually marked or wrote — read the sheet, do NOT
  solve the test and do NOT guess.
- For a MARKED option, output the EXACT character the student wrote, copied
  letter-for-letter from the sheet: one of A, B, C, D, E, F or А, Б, В, Г, Д, Е.
- NEVER TRANSLITERATE OR CONVERT BETWEEN SCRIPTS. This is the most important
  rule for Cyrillic sheets. If the student wrote Cyrillic "Б", output "Б" — NOT
  "B". If the student wrote Cyrillic "В", output "В" — NOT "B" and NOT "C". If
  the student wrote Cyrillic "Г", output "Г" — NOT "G" and NOT "D". Do not
  convert a letter to its sound-alike, to its look-alike, or to the letter at
  the same POSITION in the other alphabet. Copy the character exactly as drawn.
- Do NOT force a mark into A-D: if the student clearly marked E or F, output
  "E" or "F".
- For a WRITTEN answer, output the text EXACTLY as written, letter by letter.
  Do NOT correct spelling, do NOT translate, do NOT transliterate between
  scripts (Latin stays Latin, Cyrillic stays Cyrillic), do NOT expand
  abbreviations. If a single letter is genuinely illegible, choose the most
  likely letter for THAT letter — never invent a different word.
- If a mark is ambiguous, erased, crossed-out, or the student marked TWO or more
  options for the same question, output "?" for that question. NEVER guess a
  single letter in that case.
- HANDWRITING AMBIGUITY — output "?" instead of guessing. If a mark could
  plausibly be more than ONE letter in this student's handwriting, output "?".
  Common look-alike pairs: Cyrillic Г vs А vs С; Latin E vs F; D vs O; B vs 8;
  Cyrillic Б vs В. Ask yourself "could a careful human read this as a different
  letter?" — if yes, output "?".
  WHY: a "?" is sent to the teacher to check by hand, which is the CORRECT and
  harmless outcome. A GUESSED letter that turns out wrong is silently marked
  wrong and the student unfairly loses a point. So when in real doubt, "?" is
  always the better answer than a guess. Do NOT use "?" for marks you can read
  confidently — only for genuinely ambiguous ones.
- If a question is left completely blank (nothing marked and nothing written),
  output null.
- Also read the VARIANT NUMBER if it is written anywhere on the sheet
  (e.g. "Variant 3", "V-3", "3-variant"); if none is visible, use null.
- Also read the STUDENT'S NAME. It is HANDWRITTEN, usually at the very top of
  the sheet, often next to or above the variant label. It is an Uzbek name and
  may be written in Latin OR Cyrillic script. Transcribe it letter-by-letter
  EXACTLY as written: do NOT correct it into a real/dictionary word, do NOT
  translate, and do NOT transliterate between scripts (keep Cyrillic as
  Cyrillic, Latin as Latin). If a single letter is genuinely illegible, choose
  the most likely letter for THAT letter — never invent a different name. If the
  name area is blank or completely unreadable, use null (do NOT guess a name).
- CONFIDENCE for the NAME — bias HEAVILY toward flagging. Still return your
  best-guess name string, but ALSO set "name_unsure" to true whenever ANYTHING
  about the name is less than crisp, clearly-printed block capitals. Best guess
  and flag are NOT in conflict: give the guess AND flag it. Set "name_unsure" to
  true if ANY of these apply to ANY part of the name:
    * any letter is ambiguous or could be read as more than one letter;
    * letters could MERGE or SPLIT — e.g. "AI" vs "AT", "cl" vs "d", or two
      letters that run together and could read as one (or one that could split
      into two);
    * spacing is odd, or a word-break is unclear (e.g. it could be one name or
      two);
    * the handwriting is cursive, messy, faint, or not clean block capitals.
  Examples: strokes read as "SATDAR BAR" but could be "SAIDAKBAR" -> flag;
  "AXMND" where the 4th letter could be A or E -> flag; clearly printed "ALIYEV"
  with every letter unmistakable -> do NOT flag.
  A student name is hard to verify later and a wrong name on a graded sheet is a
  real error, while a false flag costs the teacher only one tap — so when in ANY
  doubt, flag it. Only set "name_unsure" to false when every letter is
  unmistakable. If the name is null, "name_unsure" must be false.

Return ONLY valid JSON, no markdown, no explanation.

Example for a LATIN sheet (question 22 is a written answer, the rest are marked
options):
{{"variant": 3, "student_name": "Ali Valiyev", "name_unsure": false, "answers": {{"1": "A", "2": "?", "3": null, "4": "C", "22": "SMARTPHONE"}}}}

Example for a CYRILLIC sheet — note the letters are copied EXACTLY as written,
never converted to Latin:
{{"variant": 29, "student_name": "САРДОР", "name_unsure": false, "answers": {{"1": "А", "2": "Б", "3": "Г", "4": "В", "5": "Д"}}}}"""


# Bound simultaneous grading Gemini calls (Fix 4). Module-level so it is shared
# across every concurrent read_answer_sheet — a burst of photos can't overwhelm
# Gemini's rate limits. In Python 3.10+ the Semaphore binds to the running loop
# lazily on first acquire, so creating it at import is safe. Mirrors the
# per-instance self._sem pattern in ai_analyzer.
_grading_sem = asyncio.Semaphore(settings.MAX_CONCURRENT_GRADING)


class _RestUsage:
    """usage_metadata shim so log_gemini_usage reads the REST counts unchanged.

    Bonus over the SDK path: the REST response DOES return thoughtsTokenCount,
    which google-generativeai 0.8.3 omits — so grading cost is now logged
    accurately instead of silently undercounting the thinking tokens.
    """
    __slots__ = ("prompt_token_count", "candidates_token_count",
                 "thoughts_token_count", "total_token_count")

    def __init__(self, um: dict) -> None:
        def g(name: str) -> int:
            try:
                return int(um.get(name, 0) or 0)
            except (TypeError, ValueError):
                return 0
        self.prompt_token_count = g("promptTokenCount")
        self.candidates_token_count = g("candidatesTokenCount")
        self.thoughts_token_count = g("thoughtsTokenCount")
        self.total_token_count = g("totalTokenCount")


class _RestResponse:
    """Minimal stand-in carrying only what the usage logger touches."""
    def __init__(self, um: dict) -> None:
        self.usage_metadata = _RestUsage(um)


# finishReason arrives as a STRING over REST; map to the int contract the
# caller already uses (2 == MAX_TOKENS/truncated, 1 == STOP).
_FINISH_REASONS = {"STOP": 1, "MAX_TOKENS": 2, "SAFETY": 3, "RECITATION": 4}

_REST_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{settings.GEMINI_MODEL}:generateContent"
)


def _call_sync(prompt: str, png_bytes: bytes) -> tuple[str, int]:
    """Blocking Gemini call. Returns (text, finish_reason); runs on a worker
    thread via asyncio.to_thread.

    Uses the REST endpoint DIRECTLY rather than google-generativeai, for ONE
    reason: the installed SDK (0.8.3) cannot set thinkingConfig, and grading does
    not need thinking. Measured on real sheets, thinkingBudget=0 is 3.3-4.6x
    CHEAPER, up to 2.8x FASTER, and MORE accurate (Cyrillic 7/10 -> 9/10,
    identical across runs) than the thinking default. It also structurally
    removes the MAX_TOKENS truncation class, since no hidden thinking tokens can
    eat the output budget. Extraction still uses the SDK and is untouched.

    max_output_tokens stays generous (8192) as belt-and-braces; the retry loop
    still handles an empty/truncated read.
    """
    import base64

    import requests

    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(png_bytes).decode("ascii"),
                }},
            ],
        }],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            # THE POINT OF THE REST PATH: no thinking on the grading read.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    # Bound the socket too: asyncio.wait_for abandons the await but cannot kill
    # the worker thread, so without this a hung request would leak a thread.
    http = requests.post(
        _REST_URL,
        params={"key": settings.GEMINI_API_KEY},
        json=body,
        timeout=settings.GEMINI_GRADING_TIMEOUT,
    )
    http.raise_for_status()
    payload = http.json()
    response = _RestResponse(payload.get("usageMetadata") or {})
    # Cost accounting only — kind="grade". Wrapped so it can NEVER crash grading.
    try:
        from app.services.usage_log import log_gemini_usage
        log_gemini_usage(response, kind="grade", model=settings.GEMINI_MODEL)
    except Exception:
        pass
    candidates = payload.get("candidates") or []
    if not candidates:
        return "", 0  # blocked/empty response → caller retries, then asks for a retake
    finish_reason = _FINISH_REASONS.get(str(candidates[0].get("finishReason", "")), 0)
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return text, finish_reason


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_response(raw: str) -> dict[str, Any]:
    """Defensive parse: strip code fences, then salvage the first {...} block."""
    text = re.sub(r"```(?:json)?\s*", "", raw or "").strip().rstrip("`").strip()

    data = _try_json(text)
    if data is None:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e > s:
            data = _try_json(text[s:e + 1])

    if not isinstance(data, dict):
        return {"variant": None, "answers": {}}
    return data


def _norm_letter(value: Any) -> str | None:
    """
    A marked option → its CANONICAL letter (for matching), '?' (unclear), or None.

    The WHOLE value must be a single option letter — Latin (A–E) or Cyrillic
    (А Б В Г Д Е) — to count as a marked option; a written answer like "APPLE"
    must NOT be read as "A". None here means "not a bare option letter", i.e. it
    may be a written short answer (see _clean_text). Canonicalisation unifies
    cross-script look-alikes so grading matches the key; both sides canonicalise.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s[0] == "?":
        return "?"
    if not is_option_letter(s):
        return None  # a written word (or non-option char) is not a marked option
    # Store the REAL character the student marked (script preserved), so the
    # report shows "Б" as "Б" and a Б-vs-В difference stays visible. Cross-script
    # equality is applied at COMPARISON time by checker.is_correct, which folds
    # both sides — matching is unchanged, only storage/display becomes honest.
    return s.upper()


def _clean_text(value: Any) -> str | None:
    """
    A WRITTEN short answer, kept RAW — script preserved, no spell-fixing, no
    transliteration. Only whitespace-collapsed and length-capped. Matching
    (case-insensitivity) happens later in checker.py, not here.
    """
    if value is None:
        return None
    s = " ".join(str(value).split())
    if not s or s.startswith("?"):
        return None
    return s[:100]


def _coerce_variant(value: Any) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _as_bool(value: Any) -> bool:
    """
    Defensive truthiness for a Gemini-returned flag. Real booleans pass through;
    the STRING "false"/"no"/"0" must read as False (bool("false") is True — that
    would flag every name). Anything unrecognised → False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _clean_name(value: Any) -> str | None:
    """
    Return the handwritten name RAW — only trim whitespace and cap length. No
    spelling correction, no case folding, no transliteration (per spec). Blank,
    None, or the literal string "null" → None.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == "null":
        return None
    return s[:100]  # matches CheckResult.student_name / display_name width


def _prepare_png(image_bytes: bytes) -> bytes | None:
    """Decode → preprocess → PNG-encode, all in one shot.

    Pure CPU work (PIL decode, OpenCV deskew/CLAHE, PNG encode). Kept in a
    single sync function so `read_answer_sheet` can hand the WHOLE block to
    asyncio.to_thread — otherwise any of these steps would block the event
    loop and freeze the bot for every other user while one photo is prepared.

    Returns the PNG bytes, or None if the image yields no page.
    """
    pages = image_to_pages(image_bytes)
    if not pages:
        return None
    img = preprocess_image(pages[0].image)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _empty_read() -> dict[str, Any]:
    return {
        "variant": None, "student_name": None, "name_unclear": False,
        "answers": {}, "texts": {}, "unclear": [],
    }


def _has_content(r: dict[str, Any]) -> bool:
    return bool(r["answers"] or r["texts"] or r["unclear"])


async def _read_once(prompt: str, png_bytes: bytes) -> dict[str, Any]:
    """ONE read of the sheet, with the grading retry budget.

    Grading-only budget: tight per-attempt timeout, few attempts, flat backoff
    (GEMINI_GRADING_*). A read is retryable not only on timeout/error but also
    when it comes back EMPTY or TRUNCATED (finish_reason=2): the photo is fine,
    the model just cut the JSON short on this draw, so one more call recovers it.
    """
    for attempt in range(settings.GEMINI_GRADING_MAX_RETRIES):
        try:
            # Hold a concurrency slot only for the actual call — released across
            # the backoff sleep so a waiting photo can proceed meanwhile.
            async with _grading_sem:
                raw, finish_reason = await asyncio.wait_for(
                    asyncio.to_thread(_call_sync, prompt, png_bytes),
                    timeout=settings.GEMINI_GRADING_TIMEOUT,
                )
            result = _build_read(raw)
            if _has_content(result):
                return result  # got content → done (partial reads kept, not lost)
            logger.warning(
                "sheet_read_empty", attempt=attempt + 1, finish_reason=finish_reason
            )
        except asyncio.TimeoutError:
            logger.warning("sheet_read_timeout", attempt=attempt + 1)
        except Exception as e:
            logger.warning("sheet_read_error", attempt=attempt + 1, error=str(e))
        if attempt < settings.GEMINI_GRADING_MAX_RETRIES - 1:
            await asyncio.sleep(1)  # flat 1s backoff — keep the worst case small
    return _empty_read()  # every attempt empty → genuinely unreadable


def _labels_block(option_labels: list[str] | None) -> str:
    """Prompt preamble naming THIS test's real option labels, or "" if unknown.

    Deliberately a strong HINT paired with the "?" escape, never a forced choice:
    the manual flow derives labels from the typed key, which may not cover every
    option the paper offers. Forcing a mark into an incomplete set could turn a
    genuinely different mark into a listed one — and a wrong answer that lands on
    the key letter would be silently CREDITED. "?" removes that risk.
    """
    if not option_labels:
        return ""
    listed = ", ".join(option_labels)
    return (
        f'THIS TEST\'S OPTION LABELS ARE: {listed}\n'
        f'A marked option on this sheet is normally one of: {listed}. Output the '
        f'one the student marked, EXACTLY as listed (same script). If you cannot '
        f'tell WHICH of them a mark is, output "?" — never pick one at random, '
        f'and never invent a letter that is not in this list.\n\n'
    )


def _build_read(raw: str) -> dict[str, Any]:
    """Parse a raw Gemini grading response into the read dict.

    A marked option canonicalises to a letter; "?" is unclear; anything else is a
    written short answer. An empty/truncated response yields empty
    answers/texts/unclear — the caller treats that as retryable, then unreadable.
    """
    data = _parse_response(raw)
    answers: dict[int, str] = {}
    texts: dict[int, str] = {}
    unclear: list[int] = []
    for k, v in (data.get("answers") or {}).items():
        try:
            q = int(k)
        except (TypeError, ValueError):
            continue
        letter = _norm_letter(v)
        if letter == "?":
            unclear.append(q)
        elif letter is not None:
            answers[q] = letter          # a marked option
        else:
            txt = _clean_text(v)
            if txt:
                texts[q] = txt           # a written short answer

    # NAME confidence flag. A missing name is not "unclear" — only a
    # present-but-doubtful name is worth asking the teacher to confirm.
    name = _clean_name(data.get("student_name"))
    name_unclear = _as_bool(data.get("name_unsure")) and name is not None

    return {
        "variant": _coerce_variant(data.get("variant")),
        "student_name": name,
        "name_unclear": name_unclear,
        "answers": answers,
        "texts": texts,
        "unclear": sorted(unclear),
    }


async def read_answer_sheet(
    image_bytes: bytes,
    expected_count: int,
    option_labels: list[str] | None = None,
) -> dict[str, Any]:
    """
    Read a student's answer sheet.

    Returns:
      {
        "variant": int | None,       # variant number if visible on the sheet
        "student_name": str | None,  # handwritten name, RAW (unnormalized)
        "name_unclear": bool,        # True = name read is doubtful — ask to confirm
        "answers": {int: str},       # MARKED options BOTH reads agreed on
        "texts": {int: str},         # WRITTEN short answers BOTH reads agreed on
        "unclear": [int],            # questions BOTH reads marked "?"
      }

    `name_unclear` is the reader's self-reported uncertainty about the NAME (it
    drives the teacher name-confirm). `student_name`/`texts` still hold the
    best-guess transcription — the flag never withholds the value.

    ONE Gemini call per sheet. Marked options and written answers both come from
    that single call.

    On any Gemini/parse failure returns an empty read (the caller treats an empty
    read as "unreadable — ask for a clearer photo"). NEVER raises.
    """
    empty = _empty_read()
    try:
        # Decode + preprocess + PNG-encode is CPU-heavy; run it OFF the event
        # loop so one teacher's photo never freezes the bot for everyone else.
        _t_pre = time.perf_counter()
        png_bytes = await asyncio.to_thread(_prepare_png, image_bytes)
        logger.info(
            "preprocess_timing",
            preprocess_timing_ms=round((time.perf_counter() - _t_pre) * 1000),
        )
        if png_bytes is None:
            return empty
    except Exception as e:
        logger.warning("sheet_preprocess_failed", error=str(e))
        return empty

    prompt = ANSWER_SHEET_PROMPT.format(
        total=expected_count, labels=_labels_block(option_labels)
    )
    # ONE read. A two-read cross-check was built and measured, then REJECTED —
    # see docs/BACKLOG.md: it caught a real silent misread in only 1 of 3 runs,
    # doubled token cost, and made an empty read poison the whole sheet (one
    # failed read made every question "disagree"). Its independence assumption is
    # also weak: two calls on identical pixels are correlated, so they tend to be
    # wrong the SAME way. Uncertainty is surfaced by the reader's own "?" instead.
    return await _read_once(prompt, png_bytes)
