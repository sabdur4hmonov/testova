"""AI-text detector in the bot (handlers/detector.py, services/detector_client.py).

The detector service is faked here; its own behaviour (models, Tesseract on
real clear/blurred images) is tested in detector/tests and inside its image.
"""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest

from app.bot.handlers import detector as det
from app.bot.keyboards.main_menu import DETECTOR_MENU_TEXTS, MAIN_MENU_TEXTS, main_menu
from app.config import settings
from app.services import detector_client as dc

LANGS = ("uz", "en", "ru")


# ── fakes ────────────────────────────────────────────────────────────────────

class _Status:
    def __init__(self, owner, text):
        self.owner, self.text, self.deleted = owner, text, False

    async def edit_text(self, text, **kw):
        self.text = text
        self.owner.edits.append(text)

    async def delete(self):
        self.deleted = True


class _Msg:
    _next_id = 100

    def __init__(self, text=None, photo=None, document=None, media_group_id=None):
        _Msg._next_id += 1
        self.message_id = _Msg._next_id
        self.text = text
        self.photo = photo
        self.document = document
        self.media_group_id = media_group_id
        self.chat = SimpleNamespace(id=1)
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.documents: list = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return _Status(self, text)

    async def answer_document(self, doc, caption=None, **kw):
        self.documents.append((doc, caption))

    def all_text(self) -> str:
        return "\n".join(self.answers + self.edits)


class _State:
    def __init__(self):
        self.state, self.data = None, {}

    async def set_state(self, s):
        self.state = s

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def clear(self):
        self.state, self.data = None, {}


def _user(lang="uz"):
    return SimpleNamespace(language=SimpleNamespace(value=lang), telegram_id=42,
                           is_admin=False)


def _photo(file_id="f1"):
    return [SimpleNamespace(file_id=file_id + "_small", file_size=10),
            SimpleNamespace(file_id=file_id, file_size=1000)]


def _png(blur: float = 0.0) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.new("RGB", (900, 300), "white")
    d = ImageDraw.Draw(img)
    for i in range(6):
        d.text((20, 20 + i * 40), "Maktab hayoti har bir inson uchun yorqin xotira.", fill="black")
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


WORDS_120 = " ".join(["Bu matn o'quvchi tomonidan yozilgan uzun insho edi."] * 15)


def _result(p=0.82, verdict="likely_ai", sentences=None, **kw):
    sentences = sentences if sentences is not None else [
        {"text": "Birinchi gap juda oddiy yozilgan.", "ai_probability": 0.1},
        {"text": "Ikkinchi gap <shubhali> ko'rinadi.", "ai_probability": 0.97},
        {"text": "Uchinchi gap ham shubhali.", "ai_probability": 0.9},
        {"text": "Qisqa.", "ai_probability": None},
    ]
    return {"lang": "en", "supported": True, "ai_probability": p, "verdict": verdict,
            "sentences": sentences, "model_version": "m", "word_count": 120,
            "truncated": False, "chunks": 1} | kw


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_ENABLED", True)
    monkeypatch.setattr(settings, "DETECTOR_HANDWRITING_VISION", False)


@pytest.fixture
def fake_service(monkeypatch):
    calls = {"detect": [], "ocr": []}
    box = {"detect": _result(), "ocr": None, "detect_exc": None, "ocr_exc": None}

    async def detect(text):
        calls["detect"].append(text)
        if box["detect_exc"]:
            raise box["detect_exc"]
        return box["detect"]

    async def ocr(images):
        calls["ocr"].append(images)
        if box["ocr_exc"]:
            raise box["ocr_exc"]
        return box["ocr"]

    monkeypatch.setattr(dc, "detect", detect)
    monkeypatch.setattr(dc, "ocr", ocr)

    downloads = []

    async def fetch(bot, file_id, size_bytes=0):
        downloads.append(file_id)
        return _png()

    monkeypatch.setattr(det, "fetch_telegram_file", fetch)
    return SimpleNamespace(calls=calls, box=box, downloads=downloads)


# ── flag off: the bot is what it was ────────────────────────────────────────

def test_flag_off_by_default():
    from app.config import Settings
    assert Settings.model_fields["DETECTOR_ENABLED"].default is False
    assert Settings.model_fields["DETECTOR_HANDWRITING_VISION"].default is False


def test_menu_has_no_detector_button_when_off(monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_ENABLED", False)
    for lang in LANGS:
        flat = [b.text for row in main_menu(lang).keyboard for b in row]
        assert DETECTOR_MENU_TEXTS[lang] not in flat
        assert [b.text for b in main_menu(lang).keyboard[1]] == [MAIN_MENU_TEXTS[lang]["check"]]


def test_menu_shows_button_when_on(enabled):
    for lang in LANGS:
        row = [b.text for b in main_menu(lang).keyboard[1]]
        assert row == [MAIN_MENU_TEXTS[lang]["check"], DETECTOR_MENU_TEXTS[lang]]


def test_detector_labels_do_not_collide_with_menu_labels():
    menu = {t for lang in LANGS for t in MAIN_MENU_TEXTS[lang].values()}
    assert not menu & set(DETECTOR_MENU_TEXTS.values())


# ── routing through aiogram's own resolution ────────────────────────────────

def _handler_routers():
    import importlib
    import pkgutil

    from aiogram import Router

    import app.bot.handlers as pkg
    for info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"app.bot.handlers.{info.name}")
        for value in vars(mod).values():
            if isinstance(value, Router):
                yield value


@pytest.fixture(scope="module")
def dp():
    """A real dispatcher built from the module-level routers. aiogram lets a
    router belong to one dispatcher only, and test_menu_shadowing /
    sheet_harness build their own: detach for the build, then give every
    router back the parent it had (the same approach as sheet_harness)."""
    from app.bot.main import create_dispatcher

    saved = [(r, r._parent_router) for r in set(_handler_routers())]
    for r, _parent in saved:
        r._parent_router = None
    d = create_dispatcher()
    yield d
    for r, parent in saved:
        r._parent_router = parent


async def _winner(dp, text, state):
    from tests.test_menu_shadowing import _winner as winner
    return await winner(dp, text, state)


async def test_button_ignored_when_off(dp, monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_ENABLED", False)
    assert await _winner(dp, DETECTOR_MENU_TEXTS["uz"], None) is None


async def test_button_wins_in_other_flows_when_on(dp, enabled):
    for state in (None, "UploadStates:waiting_for_test_name",
                  "BookStates:waiting_for_page_range", "CheckingStates:waiting_for_manual_name"):
        assert await _winner(dp, DETECTOR_MENU_TEXTS["uz"], state) == "detector.handle_detector_button"


async def test_menu_buttons_leave_the_detector(dp, enabled):
    from tests.test_menu_shadowing import _owner_map
    for lang in LANGS:
        owners = await _owner_map(dp, lang)
        for key, label in MAIN_MENU_TEXTS[lang].items():
            got = await _winner(dp, label, "DetectorStates:waiting_for_input")
            assert got == owners[key], (lang, key, got)


async def test_typed_text_reaches_the_detector(dp, enabled):
    assert await _winner(dp, "Bu tekshiriladigan matn", "DetectorStates:waiting_for_input") \
        == "detector.handle_text"


# ── text ────────────────────────────────────────────────────────────────────

async def test_entry_sets_state(enabled):
    msg, state = _Msg(DETECTOR_MENU_TEXTS["uz"]), _State()
    await det.handle_detector_button(msg, state, _user())
    assert state.state == det.DetectorStates.waiting_for_input
    assert "hukm emas" in msg.answers[0]


@pytest.mark.parametrize("lang,phrase", [
    ("uz", "Bu taxmin, hukm emas"), ("en", "estimate, not a verdict"), ("ru", "оценка, а не приговор"),
])
async def test_text_result_has_percent_verdict_sentences_and_disclaimer(enabled, fake_service, lang, phrase):
    msg = _Msg(WORDS_120)
    await det.handle_text(msg, _State(), _user(lang))
    out = msg.all_text()
    assert "82%" in out
    assert phrase in out
    assert {"uz": "Gap darajasidagi", "en": "Sentence-level", "ru": "по предложениям"}[lang] in out
    assert "&lt;shubhali&gt;" in out            # sentence text is HTML-escaped
    assert out.index("97%") < out.index("90%")  # most suspicious first
    assert "Birinchi gap" not in out            # a 10% sentence is not "suspicious"
    assert fake_service.calls["detect"] == [WORDS_120]


async def test_too_short_has_no_percent(enabled, fake_service):
    fake_service.box["detect"] = _result(p=None, verdict="too_short", sentences=[], word_count=12)
    msg = _Msg("qisqa matn")
    await det.handle_text(msg, _State(), _user())
    assert "12 so'z" in msg.all_text() and "%" not in msg.all_text()


async def test_unsupported_language_has_no_percent(enabled, fake_service):
    fake_service.box["detect"] = _result(p=None, verdict="unsupported", sentences=[], supported=False)
    msg = _Msg(WORDS_120)
    await det.handle_text(msg, _State(), _user())
    assert "tayyor emas" in msg.all_text() and "%" not in msg.all_text()


async def test_service_down_is_a_message_not_a_crash(enabled, fake_service):
    fake_service.box["detect_exc"] = dc.DetectorUnavailable("ConnectError")
    msg, state = _Msg(WORDS_120), _State()
    await det.handle_text(msg, state, _user())
    assert "ishlamayapti" in msg.all_text()
    # the single-flight lock was released: the next text is analysed
    fake_service.box["detect_exc"] = None
    msg2 = _Msg(WORDS_120)
    await det.handle_text(msg2, state, _user())
    assert "82%" in msg2.all_text()


async def test_service_busy(enabled, fake_service):
    fake_service.box["detect_exc"] = dc.DetectorBusy()
    msg = _Msg(WORDS_120)
    await det.handle_text(msg, _State(), _user())
    assert "band" in msg.all_text()


async def test_real_client_unreachable_raises_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(settings, "DETECTOR_TIMEOUT", 2.0)
    with pytest.raises(dc.DetectorUnavailable):
        await dc.detect("x")


# ── files ───────────────────────────────────────────────────────────────────

def test_extract_text_docx_txt_pdf():
    from docx import Document
    import fitz

    d = Document()
    d.add_paragraph("Salom dunyo")
    buf = io.BytesIO()
    d.save(buf)
    assert dc.extract_text("a.docx", None, buf.getvalue()) == "Salom dunyo"
    assert dc.extract_text("a.txt", None, "Привет".encode("cp1251")) == "Привет"
    pdf = fitz.open()
    pdf.new_page().insert_text((50, 50), "Hello world")
    text = dc.extract_text("a.pdf", None, pdf.tobytes())
    assert "Hello world" in text and "=== Page" not in text
    with pytest.raises(dc.UnsupportedFile):
        dc.extract_text("a.xlsx", None, b"x")


async def test_document_unsupported_type(enabled, fake_service):
    doc = SimpleNamespace(file_id="d", file_size=10, file_name="a.xlsx", mime_type="application/x")
    msg = _Msg(document=doc)
    await det.handle_document(msg, _State(), _user(), bot=None)
    assert "PDF, DOCX, TXT" in msg.all_text()
    assert fake_service.calls["detect"] == []


async def test_document_txt_is_analysed(enabled, fake_service, monkeypatch):
    async def fetch(bot, file_id, size_bytes=0):
        return WORDS_120.encode("utf-8")
    monkeypatch.setattr(det, "fetch_telegram_file", fetch)
    doc = SimpleNamespace(file_id="d", file_size=10, file_name="insho.txt", mime_type="text/plain")
    msg = _Msg(document=doc)
    await det.handle_document(msg, _State(), _user(), bot=None)
    assert fake_service.calls["detect"] == [WORDS_120]


async def test_scanned_pdf_asks_for_photos(enabled, fake_service, monkeypatch):
    import fitz
    pdf = fitz.open()
    pdf.new_page()

    async def fetch(bot, file_id, size_bytes=0):
        return pdf.tobytes()
    monkeypatch.setattr(det, "fetch_telegram_file", fetch)
    doc = SimpleNamespace(file_id="d", file_size=10, file_name="scan.pdf", mime_type="application/pdf")
    msg = _Msg(document=doc)
    await det.handle_document(msg, _State(), _user(), bot=None)
    assert "skaner" in msg.all_text()


# ── photos ──────────────────────────────────────────────────────────────────

def _ocr(text=WORDS_120, conf=91.0, low=False):
    return {"text": text, "mean_confidence": conf, "low_confidence": low,
            "word_count": dc.word_count(text), "pages": []}


async def test_clear_printed_photo_is_scored_and_text_shown(enabled, fake_service):
    fake_service.box["ocr"] = _ocr()
    msg = _Msg(photo=_photo())
    await det.handle_photo(msg, _State(), _user(), bot=None)
    assert fake_service.downloads == ["f1"]                 # the largest size
    assert len(fake_service.calls["ocr"]) == 1
    sent = fake_service.calls["ocr"][0][0]
    assert sent[:8] == b"\x89PNG\r\n\x1a\n"                  # preprocessed, as PNG
    assert fake_service.calls["detect"] == [WORDS_120]
    out = msg.all_text()
    assert "82%" in out and "<u><b>" in out                 # read text, suspicious marked
    assert "hukm emas" in out


async def test_blurred_photo_is_not_scored(enabled, fake_service):
    fake_service.box["ocr"] = _ocr(text="M@kt b h yot", conf=31.0, low=True)
    msg = _Msg(photo=_photo())
    await det.handle_photo(msg, _State(), _user(), bot=None)
    out = msg.all_text()
    assert "rasm aniq emas yoki matn juda qisqa" in out
    assert "%" not in out
    assert fake_service.calls["detect"] == []


async def test_clear_but_short_photo(enabled, fake_service):
    fake_service.box["ocr"] = _ocr(text="Bu juda qisqa matn edi.", conf=93.0, low=False)
    msg = _Msg(photo=_photo())
    await det.handle_photo(msg, _State(), _user(), bot=None)
    assert "matn juda qisqa (5 so'z" in msg.all_text()
    assert fake_service.calls["detect"] == []


async def test_handwriting_with_vision_off_says_not_supported(enabled, fake_service, monkeypatch):
    called = []
    monkeypatch.setattr(dc, "transcribe_handwriting", lambda *a: called.append(a))
    fake_service.box["ocr"] = _ocr(text="xz qw", conf=22.0, low=True)
    msg = _Msg(photo=_photo())
    await det.handle_photo(msg, _State(), _user(), bot=None)
    assert "qo'lyozma hozircha qo'llanmaydi, matnni terib yuboring" in msg.all_text()
    assert called == [] and fake_service.calls["detect"] == []


async def test_handwriting_with_vision_on_transcribes_and_logs(enabled, fake_service, monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_HANDWRITING_VISION", True)
    from app.services import gemini_rest
    calls = []

    def fake_call(prompt, **kw):
        calls.append((prompt, kw))
        return WORDS_120, 1
    monkeypatch.setattr(gemini_rest, "call", fake_call)
    monkeypatch.setattr(gemini_rest, "paid_key", lambda: "k")
    fake_service.box["ocr"] = _ocr(text="xz qw", conf=22.0, low=True)
    msg = _Msg(photo=_photo())
    await det.handle_photo(msg, _State(), _user(), bot=None)
    assert len(calls) == 1
    prompt, kw = calls[0]
    assert "Transcribe" in prompt and "Do not correct" in prompt
    assert kw["role"] == "vision" and kw["kind"] == "detector_ocr" and kw["user_id"] == 42
    assert kw["images"][0][:8] == b"\x89PNG\r\n\x1a\n"     # the photo bytes as downloaded
    assert fake_service.calls["detect"] == [WORDS_120]
    assert "qo'lyozmadan avtomatik o'qildi" in msg.all_text()


async def test_vision_failure_degrades_to_unreadable(enabled, fake_service, monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_HANDWRITING_VISION", True)

    async def boom(*a):
        raise RuntimeError("quota")
    monkeypatch.setattr(dc, "transcribe_handwriting", boom)
    fake_service.box["ocr"] = _ocr(text="xz", conf=10.0, low=True)
    msg = _Msg(photo=_photo())
    await det.handle_photo(msg, _State(), _user(), bot=None)
    assert "rasm aniq emas" in msg.all_text()


async def test_album_is_one_text_in_sent_order(enabled, fake_service, monkeypatch):
    monkeypatch.setattr(det, "albums", det._AlbumCollector(settle=0.05))
    fake_service.box["ocr"] = _ocr()
    msgs = [_Msg(photo=_photo(f"p{i}"), media_group_id="G") for i in range(3)]
    state = _State()
    # updates may be handled out of order; message_id decides
    await asyncio.gather(*(det.handle_photo(m, state, _user(), bot=None)
                           for m in (msgs[2], msgs[0], msgs[1])))
    assert len(fake_service.calls["ocr"]) == 1
    assert len(fake_service.calls["ocr"][0]) == 3
    assert fake_service.downloads == ["p0", "p1", "p2"]
    assert len(fake_service.calls["detect"]) == 1


async def test_ocr_service_down(enabled, fake_service):
    fake_service.box["ocr_exc"] = dc.DetectorUnavailable("x")
    msg = _Msg(photo=_photo())
    await det.handle_photo(msg, _State(), _user(), bot=None)
    assert "ishlamayapti" in msg.all_text()


def test_preprocess_photo_real_images():
    clear = dc.preprocess_photo(_png())
    blurred = dc.preprocess_photo(_png(blur=3))
    assert clear[:4] == blurred[:4] == b"\x89PNG"


def test_long_read_text_goes_to_a_file():
    sents = [{"text": f"Gap raqami {i} shu yerda turibdi va u ancha uzun.", "ai_probability": 0.9}
             for i in range(400)]
    body = det.marked_text({"sentences": sents}, html_mode=True)
    assert len(det._split_html(body)) > 3
    plain = det.marked_text({"sentences": sents}, html_mode=False)
    assert "[[" in plain


async def test_long_read_text_sent_as_document(enabled):
    sents = [{"text": f"Gap raqami {i} shu yerda turibdi va u ancha uzun.", "ai_probability": 0.9}
             for i in range(400)]
    msg = _Msg()
    await det._send_marked_text(msg, "uz", {"sentences": sents})
    assert len(msg.documents) == 1
