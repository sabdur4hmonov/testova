"""Talking to the AI-text detector service (detector/), and getting text out
of what a user sends.

The bot never runs a model itself: it extracts text with the libraries it
already has (PyMuPDF, python-docx), preprocesses photos with the OpenCV helper
it already has, and asks the service. Every failure of the service becomes a
DetectorUnavailable / DetectorBusy the handler turns into a plain message — a
missing or slow detector must never take a handler down.
"""
from __future__ import annotations

import asyncio
import re

import httpx

from app.config import secret_value, settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024   # Telegram's own bot download limit
# The service refuses more than 200k characters (HTTP 422) and scores at most
# its first 6000 words anyway: send less, so a long file is "truncated", not
# "detector unavailable".
MAX_SEND_CHARS = 150_000
# A photo sent as a FILE can be any size; decoding a 100 MP image costs ~300 MB
# before it is shrunk. Telegram photos are <= 1280 px, far below this.
MAX_PHOTO_PIXELS = 40_000_000


class DetectorUnavailable(RuntimeError):
    """Service down, unreachable, timed out, or answered with an error."""


class DetectorBusy(RuntimeError):
    """Service up but its queue is full (HTTP 503 "busy")."""


class UnsupportedFile(ValueError):
    pass


def _headers() -> dict[str, str]:
    token = secret_value(settings.DETECTOR_API_TOKEN)
    return {"X-Detector-Token": token} if token else {}


async def _post(path: str, **kw) -> dict:
    url = settings.DETECTOR_URL.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=settings.DETECTOR_TIMEOUT) as client:
            resp = await client.post(url, headers=_headers(), **kw)
    except (httpx.HTTPError, OSError) as e:
        logger.warning("detector_unreachable", path=path, error_type=type(e).__name__)
        raise DetectorUnavailable(type(e).__name__) from e
    if resp.status_code == 503 and "busy" in resp.text:
        raise DetectorBusy()
    if resp.status_code >= 400:
        logger.warning("detector_http_error", path=path, status=resp.status_code,
                       body=resp.text[:200])
        raise DetectorUnavailable(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise DetectorUnavailable("bad json") from e


async def detect(text: str) -> dict:
    if len(text) > MAX_SEND_CHARS:
        cut = text.rfind(" ", 0, MAX_SEND_CHARS)
        text = text[: cut if cut > MAX_SEND_CHARS // 2 else MAX_SEND_CHARS]
    return await _post("/detect", json={"text": text})


async def ocr(images: list[bytes]) -> dict:
    files = [("files", (f"page{i + 1}.png", blob, "image/png")) for i, blob in enumerate(images)]
    return await _post("/ocr", files=files)


# Same rule as the service (detector/service/lang.py): letters only, and an
# apostrophe-joined Uzbek word (o'qituvchi) counts once.
_WORD = re.compile(r"[^\W\d_]+(?:['ʻʼ‘’`][^\W\d_]+)*")


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


# ── text from files ─────────────────────────────────────────────────────────

_PAGE_HEADER = re.compile(r"^=== Page \d+ ===$", re.MULTILINE)


def _decode_txt(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def extract_text(filename: str, mime: str | None, data: bytes) -> str:
    """Text of a PDF / DOCX / TXT upload. Raises UnsupportedFile otherwise.
    A scanned PDF (no text layer) comes back as ""."""
    from app.services.file_processor import docx_extract_text, pdf_extract_pages_text

    name = (filename or "").lower()
    mime = (mime or "").lower()
    if name.endswith(".pdf") or mime == "application/pdf":
        pages = pdf_extract_pages_text(data)
        return _PAGE_HEADER.sub("", "\n\n".join(pages)).strip()
    if name.endswith(".docx") or "wordprocessingml" in mime:
        return docx_extract_text(data)
    if name.endswith(".txt") or mime.startswith("text/plain"):
        return _decode_txt(data)
    raise UnsupportedFile(name or mime)


def preprocess_photo(data: bytes) -> bytes:
    """Deskew + CLAHE with the bot's existing helper, as PNG for Tesseract."""
    import io

    from PIL import Image

    from app.services.file_processor import image_to_bytes, preprocess_image

    img = Image.open(io.BytesIO(data))      # header only until convert()
    w, h = img.size
    if w * h > MAX_PHOTO_PIXELS:
        raise ValueError(f"image too large: {w}x{h}")
    return image_to_bytes(preprocess_image(img.convert("RGB")), "PNG")


async def preprocess_photos(blobs: list[bytes]) -> list[bytes]:
    return await asyncio.to_thread(lambda: [preprocess_photo(b) for b in blobs])


# ── handwriting: Gemini Vision, transcription only ──────────────────────────

HANDWRITING_PROMPT = (
    "Transcribe the handwritten text in these images exactly as written, image "
    "by image in the given order, in its original language and script. Do not "
    "correct spelling, translate, summarize, explain or comment. Output only the "
    "transcribed text. If nothing is readable, output exactly: [UNREADABLE]"
)


def _transcribe_sync(images: list[bytes], telegram_id: int | None) -> str:
    from app.services import gemini_rest

    text, _finish = gemini_rest.call(
        HANDWRITING_PROMPT,
        api_key=gemini_rest.paid_key(),
        images=images,
        image_mime="image/jpeg",
        role="vision",
        # gemini_rest logs every call to gemini_usage under this kind.
        kind="detector_ocr",
        call_site="detector.handwriting",
        user_id=telegram_id,
        temperature=0.0,
        json_response=False,
    )
    text = (text or "").strip()
    return "" if text == "[UNREADABLE]" else text


async def transcribe_handwriting(images: list[bytes], telegram_id: int | None) -> str:
    return await asyncio.to_thread(_transcribe_sync, images, telegram_id)
