"""Text helpers: word counting, cleanup, sentence splitting."""
from __future__ import annotations

import re
import unicodedata

from detector.service.lang import words

# Sentence end: . ! ? … (optionally followed by closing quotes/brackets), then
# whitespace, then something that can start a sentence. A newline pair always
# ends a sentence (paragraph break).
_SENT_END = re.compile(
    r"(?<=[.!?…])[\"»”')\]]*\s+(?=[\"«“(\[]?[A-ZА-ЯЁЎҚҒҲ0-9])"
)
_PARA_BREAK = re.compile(r"\n\s*\n")
# Common abbreviations that end in a period but do not end a sentence.
_ABBREV = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|vs|etc|e\.g|i\.e|т\.е|т\.д|т\.п|г|гг|в|вв|"
    r"им|ул|стр|рис|ya'ni|y|b|m)\.$",
    re.IGNORECASE,
)


def clean_text(text: str) -> str:
    """NFC, unify line endings, drop control characters, collapse runs of
    spaces. Paragraph breaks are kept: they are sentence boundaries."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or unicodedata.category(ch)[0] != "C"
    )
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(words(text))


def truncate_words(text: str, max_words: int) -> tuple[str, bool]:
    """First `max_words` words of `text` (cut at a word end) and whether
    anything was cut."""
    count = 0
    for m in re.finditer(r"\S+", text):
        if words(m.group(0)):
            count += 1
            if count == max_words:
                return text[: m.end()], m.end() < len(text.rstrip())
    return text, False


def split_sentences(text: str) -> list[str]:
    """Sentences in order. Lines without terminal punctuation (headings, list
    items) come back as their own sentence."""
    out: list[str] = []
    for para in _PARA_BREAK.split(text):
        para = para.strip()
        if not para:
            continue
        for line in para.split("\n"):
            line = line.strip()
            if not line:
                continue
            pieces = _SENT_END.split(line)
            buf = ""
            for piece in pieces:
                buf = f"{buf} {piece}".strip() if buf else piece.strip()
                if _ABBREV.search(buf):
                    continue  # "Dr. Smith" — keep gluing
                if buf:
                    out.append(buf)
                buf = ""
            if buf:
                out.append(buf)
    return out
