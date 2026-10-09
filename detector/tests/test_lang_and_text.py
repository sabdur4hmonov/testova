from detector.service.lang import EN, RU, UNKNOWN, UZ, UZ_CYRL, detect_language
from detector.service.text import clean_text, split_sentences, truncate_words, word_count

EN_TEXT = (
    "The history of the region is closely connected with the Silk Road, and "
    "for centuries merchants travelled through its cities with goods from "
    "China and Persia. It was a place where many cultures met."
)
RU_TEXT = (
    "История этого края тесно связана с Великим шёлковым путём, и на "
    "протяжении веков купцы проходили через его города. Это было место, где "
    "встречались разные культуры, и оно до сих пор хранит их следы."
)
UZ_TEXT = (
    "Bu o'lkaning tarixi Buyuk ipak yo'li bilan chambarchas bog'liq va "
    "asrlar davomida savdogarlar uning shaharlari orqali o'tgan. Bu turli "
    "madaniyatlar uchrashgan joy bo'lib, hozir ham ularning izlarini saqlaydi."
)
UZ_TEXT_OKINA = UZ_TEXT.replace("'", "ʻ")
UZ_CYRL_TEXT = (
    "Бу ўлканинг тарихи Буюк ипак йўли билан чамбарчас боғлиқ ва асрлар "
    "давомида савдогарлар унинг шаҳарлари орқали ўтган. Бу турли маданиятлар "
    "учрашган жой бўлиб, ҳозир ҳам уларнинг изларини сақлайди."
)


def test_detects_four_targets():
    assert detect_language(EN_TEXT) == EN
    assert detect_language(RU_TEXT) == RU
    assert detect_language(UZ_TEXT) == UZ
    assert detect_language(UZ_TEXT_OKINA) == UZ
    assert detect_language(UZ_CYRL_TEXT) == UZ_CYRL


def test_unknown_for_other_or_empty():
    assert detect_language("") == UNKNOWN
    assert detect_language("12 34 56 78") == UNKNOWN
    # German: Latin script, none of our stop words dominate
    assert detect_language(
        "Der Hund läuft schnell über die Straße und bellt laut nach Hause"
    ) == UNKNOWN


def test_uzbek_text_with_english_quote_stays_uzbek():
    text = UZ_TEXT + ' Muallif "the past is a foreign country" deb yozgan.'
    assert detect_language(text) == UZ


def test_word_count_keeps_apostrophe_words_whole():
    assert word_count("o'qituvchi g'alaba qildi") == 3
    assert word_count("oʻqituvchi gʻalaba") == 2


def test_split_sentences():
    s = split_sentences("Birinchi gap. Ikkinchi gap! Uchinchi?\n\nSarlavha\nYana bir gap.")
    assert s == ["Birinchi gap.", "Ikkinchi gap!", "Uchinchi?", "Sarlavha", "Yana bir gap."]


def test_split_sentences_keeps_abbreviations():
    s = split_sentences("We met Dr. Smith today. He was late.")
    assert s == ["We met Dr. Smith today.", "He was late."]


def test_split_sentences_cyrillic():
    assert split_sentences("Это первое. Это второе.") == ["Это первое.", "Это второе."]


def test_truncate_words():
    text = "one two three four five"
    assert truncate_words(text, 3) == ("one two three", True)
    assert truncate_words(text, 10) == (text, False)


def test_clean_text_normalizes():
    assert clean_text("a\r\nb\x00c   d\n\n\n\ne") == "a\nbc d\n\ne"
