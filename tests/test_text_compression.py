"""OPT4 — safe theme-text compression: fewer generation-input tokens with
ZERO fact loss. The quality-protection tests (numbered list item + bare numeric
fact survive) are mandated, not optional."""
from app.services.book_generator import compress_theme_text


def test_collapses_whitespace_and_blank_lines():
    text = "Yurak   qon   aylantiradi.\n\n\n\nMiokard  muskul."
    out = compress_theme_text(text)
    assert "   " not in out
    assert "\n\n\n" not in out
    assert "Yurak qon aylantiradi." in out
    assert "Miokard muskul." in out


def test_running_header_is_stripped():
    # A short non-sentence line repeated on every page is a running header.
    text = "\n".join([
        "BIOLOGIYA 8", "Yurak to'rt kameradan iborat.",
        "BIOLOGIYA 8", "Chap qorincha aortaga haydaydi.",
        "BIOLOGIYA 8", "Miokard yurak muskuli.",
    ])
    out = compress_theme_text(text)
    assert "BIOLOGIYA 8" not in out
    assert "Yurak to'rt kameradan iborat." in out
    assert "Miokard yurak muskuli." in out


def test_page_number_run_is_stripped():
    text = "\n".join([
        "Birinchi sahifa matni.", "45",
        "Ikkinchi sahifa matni.", "46",
        "Uchinchi sahifa matni.", "47",
    ])
    out = compress_theme_text(text)
    assert "\n45" not in out and out.split() != []
    for pn in ("45", "46", "47"):
        assert pn not in out.split("\n")
    assert "Ikkinchi sahifa matni." in out


# ── QUALITY PROTECTION (mandated) ────────────────────────────────────────────

def test_numbered_list_item_survives():
    text = "\n".join([
        "Ro'yxat:", "1. Birinchi element", "2. Ikkinchi element",
        "3. Uchinchi element",
    ])
    out = compress_theme_text(text)
    # numbered list items are NOT bare integers — every one must survive intact
    assert "1. Birinchi element" in out
    assert "2. Ikkinchi element" in out
    assert "3. Uchinchi element" in out


def test_isolated_bare_numeric_fact_survives():
    # A lone bare number (e.g. an answer or a datum) is NOT a page-number run.
    text = "\n".join([
        "O'rgimchakning oyoqlari soni:", "8",
        "Bu muhim fakt.",
    ])
    out = compress_theme_text(text)
    assert "8" in out.split("\n"), "a lone numeric fact was wrongly stripped"


def test_two_bare_numbers_are_not_a_run():
    # Fewer than 3 bare integers must never be treated as page numbers.
    text = "\n".join(["Fakt bir: 100.", "12", "Fakt ikki.", "13"])
    out = compress_theme_text(text)
    assert "12" in out.split("\n") and "13" in out.split("\n")


def test_factual_sentences_are_all_preserved():
    facts = [
        "Yurak to'rt kameradan iborat.",
        "Chap qorincha qonni aortaga haydaydi.",
        "O'ng qorincha qonni o'pka arteriyasiga yuboradi.",
    ]
    out = compress_theme_text("\n".join(facts))
    for f in facts:
        assert f in out


def test_empty_input_is_safe():
    assert compress_theme_text("") == ""
    assert compress_theme_text(None) is None
