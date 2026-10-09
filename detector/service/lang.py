"""Language identification without a model.

Four targets only: English, Russian, Uzbek Latin, Uzbek Cyrillic. A model
(fastText, lingua) would cost RAM for 170 languages we never serve; this module
uses the script, the letters only Uzbek has, and short stop-word lists, which
separates these four reliably on the paragraph-length texts the detector
accepts (80+ words). Anything it cannot place is "unknown" — the caller then
answers `supported=false` rather than guessing.
"""
from __future__ import annotations

import re

EN = "en"
RU = "ru"
UZ = "uz"            # Uzbek, Latin script
UZ_CYRL = "uz-cyrl"  # Uzbek, Cyrillic script
UNKNOWN = "unknown"
MIXED = "mixed"      # Latin and Cyrillic both substantial: no single model fits

# Share of letters in the minority script above which a text is MIXED. Below
# it, a few quoted sentences in the other script do not change the language.
MIXED_SHARE = 0.25

_WORD_RE = re.compile(r"[^\W\d_]+(?:['ʻʼ‘’`][^\W\d_]+)*", re.UNICODE)

# Letters that exist in Uzbek Cyrillic and not in Russian.
_UZ_CYRL_LETTERS = set("ўқғҳЎҚҒҲ")
# Russian letters Uzbek Cyrillic does not use (щ/ы/ь occur in Russian loans in
# Uzbek too, so they only nudge the score, never decide alone).
_RU_ONLY_LETTERS = set("щыьЩЫЬ")

# o' / g' with any of the apostrophe shapes people actually type.
_UZ_LATIN_DIGRAPH = re.compile(r"[ogOG]['ʻʼ‘’`]")

_STOP = {
    EN: set("""the and of to in is that it for was on are as with be by this
        have from or an they which you were not but their at his her has had
        we can will would there been one all more so if what when about also
        into than them these its may such some other only""".split()),
    RU: set("""и в не на что я с он как а по это она к но они мы из у за от
        же так для было бы вы его о только уже ее все при еще был если или
        чтобы были этого когда даже может также который которые этом очень
        более того были между свою""".split()),
    UZ: set("""va bu bilan uchun ham bir u edi esa deb emas yoki lekin biz
        siz ular men sen bo'lib bo'ladi bo'lgan qilib kerak shu har hamma
        juda yana keyin uning ularning bizning o'z o'zining qanday nima
        mumkin hisoblanadi orqali bo'yicha tomonidan haqida ekan""".split()),
    UZ_CYRL: set("""ва бу билан учун ҳам бир у эди эса деб эмас ёки лекин биз
        сиз улар мен сен бўлиб бўлади бўлган қилиб керак шу ҳар ҳамма жуда
        яна кейин унинг уларнинг бизнинг ўз ўзининг қандай нима мумкин
        ҳисобланади орқали бўйича томонидан ҳақида экан""".split()),
}


def _normalize_apostrophes(word: str) -> str:
    return re.sub(r"['ʻʼ‘’`]", "'", word)


def words(text: str) -> list[str]:
    """Letter-only tokens; apostrophe-joined Uzbek words stay whole."""
    return _WORD_RE.findall(text or "")


def _script_counts(text: str) -> tuple[int, int]:
    cyr = lat = 0
    for ch in text:
        if "Ѐ" <= ch <= "ӿ":
            cyr += 1
        elif ch.isascii() and ch.isalpha():
            lat += 1
    return cyr, lat


def script_of(text: str) -> str | None:
    """"cyrillic" / "latin" by majority of letters, None if no letters."""
    cyr, lat = _script_counts(text)
    if cyr + lat == 0:
        return None
    return "cyrillic" if cyr > lat else "latin"


def detect_language(text: str) -> str:
    """One of EN / RU / UZ / UZ_CYRL / MIXED / UNKNOWN."""
    toks = words(text)
    if len(toks) < 5:
        return UNKNOWN

    cyr, lat = _script_counts(text)
    if cyr + lat == 0:
        return UNKNOWN
    if min(cyr, lat) / (cyr + lat) > MIXED_SHARE:
        return MIXED

    lowered = [_normalize_apostrophes(t.lower()) for t in toks]

    if cyr > lat:
        uz_letters = sum(ch in _UZ_CYRL_LETTERS for ch in text)
        ru_letters = sum(ch in _RU_ONLY_LETTERS for ch in text)
        uz_stop = sum(t in _STOP[UZ_CYRL] for t in lowered)
        ru_stop = sum(t in _STOP[RU] for t in lowered)
        # One Uzbek-only letter per ~40 words is already a strong signal;
        # Russian never produces ў/қ/ғ/ҳ.
        uz_score = uz_letters * 3 + uz_stop * 2
        ru_score = ru_letters + ru_stop * 2
        if uz_score == 0 and ru_score == 0:
            return UNKNOWN
        return UZ_CYRL if uz_score > ru_score else RU

    uz_digraphs = len(_UZ_LATIN_DIGRAPH.findall(text))
    uz_stop = sum(t in _STOP[UZ] for t in lowered)
    en_stop = sum(t in _STOP[EN] for t in lowered)
    # "sh"/"ch"/"q" without "u" are frequent in Uzbek and rare in English.
    uz_q = sum(1 for t in lowered if "q" in t and "qu" not in t)
    uz_score = uz_stop * 2 + uz_digraphs * 2 + uz_q
    en_score = en_stop * 2
    if uz_score == 0 and en_score == 0:
        return UNKNOWN
    # Require a margin: a short English quote inside an Uzbek essay (or the
    # reverse) must not flip the whole text.
    if uz_score > en_score * 1.2:
        return UZ
    if en_score > uz_score * 1.2:
        return EN
    return UNKNOWN
