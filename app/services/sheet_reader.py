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
from typing import Any

import google.generativeai as genai

from app.config import settings
from app.services.file_processor import image_to_pages, preprocess_image
from app.utils.logging import get_logger

logger = get_logger(__name__)

genai.configure(api_key=settings.GEMINI_API_KEY)

# Cyrillic look-alikes → Latin, same folding the key parser uses. A student may
# have marked А/В/С/Д on a Cyrillic form.
_CYRILLIC_MAP = {"А": "A", "В": "B", "С": "C", "Д": "D", "Е": "E"}
_VALID = {"A", "B", "C", "D"}

# NEW prompt — do NOT reuse VISION_PROMPT. Reads MARKED answers, never guesses.
ANSWER_SHEET_PROMPT = """You are reading a photo of a student's exam ANSWER SHEET.
The test has {total} questions. For MOST questions the student marks ONE option:
A, B, C or D. Some questions have NO options — for those the student WRITES a
short answer by hand (a word, a number, or a very short phrase), usually in
BLOCK CAPITAL LETTERS.

Rules:
- Report ONLY what the student actually marked or wrote — read the sheet, do NOT
  solve the test and do NOT guess.
- For a MARKED option, output just the letter: "A", "B", "C" or "D".
- For a WRITTEN answer, output the text EXACTLY as written, letter by letter.
  Do NOT correct spelling, do NOT translate, do NOT transliterate between
  scripts (Latin stays Latin, Cyrillic stays Cyrillic), do NOT expand
  abbreviations. If a single letter is genuinely illegible, choose the most
  likely letter for THAT letter — never invent a different word.
- If a mark is ambiguous, erased, crossed-out, or the student marked TWO or more
  options for the same question, output "?" for that question. NEVER guess a
  single letter in that case.
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

Return ONLY valid JSON, no markdown, no explanation. Question 22 below is a
written answer; the rest are marked options:
{{"variant": 3, "student_name": "Ali Valiyev", "answers": {{"1": "A", "2": "?", "3": null, "4": "C", "22": "SMARTPHONE"}}}}"""


_model: genai.GenerativeModel | None = None


def _get_model() -> genai.GenerativeModel:
    global _model
    if _model is None:
        _model = genai.GenerativeModel(settings.GEMINI_MODEL)
    return _model


def _call_sync(prompt: str, png_bytes: bytes) -> str:
    """Blocking Gemini call. Runs on a worker thread via asyncio.to_thread."""
    model = _get_model()
    response = model.generate_content(
        [prompt, {"mime_type": "image/png", "data": png_bytes}],
        generation_config=genai.GenerationConfig(
            temperature=0.0,
            max_output_tokens=2048,
            response_mime_type="application/json",
        ),
    )
    # Cost accounting only — kind="grade". Wrapped so it can NEVER crash grading.
    try:
        from app.services.usage_log import log_gemini_usage
        log_gemini_usage(response, kind="grade", model=settings.GEMINI_MODEL)
    except Exception:
        pass
    return response.text


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
    Fold a raw answer to 'A'..'D', '?' (unclear), or None.

    The WHOLE value must be a single letter to count as a marked option — a
    written answer like "APPLE" must NOT be read as "A". None here means "not a
    bare letter", i.e. it may be a written short answer (see _clean_text).
    """
    if value is None:
        return None
    s = str(value).strip().upper()
    if not s:
        return None
    if s[0] == "?":
        return "?"
    if len(s) != 1:
        return None  # a written word is not an option letter
    ch = _CYRILLIC_MAP.get(s, s)
    return ch if ch in _VALID else None


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


async def read_answer_sheet(
    image_bytes: bytes, expected_count: int
) -> dict[str, Any]:
    """
    Read a student's answer sheet.

    Returns:
      {
        "variant": int | None,       # variant number if visible on the sheet
        "student_name": str | None,  # handwritten name, RAW (unnormalized)
        "answers": {int: "A".."D"},  # confidently-read MARKED options
        "texts": {int: str},         # WRITTEN short answers, RAW (unnormalized)
        "unclear": [int],            # questions marked "?" (ambiguous/blank-mark)
      }

    Marked options and written answers come from the SAME single vision call —
    there is no second Gemini request.

    On any Gemini/parse failure returns empty answers/texts/unclear (the caller
    treats an empty read as "unreadable — ask for a clearer photo"). NEVER raises.
    """
    empty = {
        "variant": None, "student_name": None,
        "answers": {}, "texts": {}, "unclear": [],
    }
    try:
        pages = image_to_pages(image_bytes)
        if not pages:
            return empty
        img = preprocess_image(pages[0].image)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        png_bytes = buf.getvalue()
    except Exception as e:
        logger.warning("sheet_preprocess_failed", error=str(e))
        return empty

    prompt = ANSWER_SHEET_PROMPT.format(total=expected_count)
    raw = ""
    for attempt in range(settings.GEMINI_MAX_RETRIES):
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_call_sync, prompt, png_bytes), timeout=90
            )
            break
        except asyncio.TimeoutError:
            logger.warning("sheet_read_timeout", attempt=attempt + 1)
        except Exception as e:
            logger.warning("sheet_read_error", attempt=attempt + 1, error=str(e))
        if attempt < settings.GEMINI_MAX_RETRIES - 1:
            await asyncio.sleep(2 ** attempt)
    else:
        return empty

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

    return {
        "variant": _coerce_variant(data.get("variant")),
        "student_name": _clean_name(data.get("student_name")),
        "answers": answers,
        "texts": texts,
        "unclear": sorted(unclear),
    }
