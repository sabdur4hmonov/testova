"""Defect 3 wiring: process_file's DOCX routing.

Proves the three behaviours that matter:
  1. a shaped DOCX is converted and flows through the PDF crop path;
  2. a shape-free DOCX is NEVER converted and renders exactly as before
     (docx_to_images called with the original bytes) — the byte-identical
     bypass, by construction;
  3. a conversion FAILURE falls back to that same text render — visible
     degradation, never a crash.

All collaborators are stubbed so only the routing is under test.
"""
from PIL import Image

from app.services import pipeline
from app.services.file_processor import PageImage


class _FakeAnalyzer:
    def __init__(self, user_id=None):
        self.user_id = user_id

    async def extract_all_questions(self, images, page_infos=None):
        return [{
            "question_number": 1, "question_text": "q", "options": {},
            "has_image": False, "page_number": 1, "section": 1,
        }]

    async def ensure_scheme_content(self, *a):
        return []

    async def reextract_questions(self, *a):
        return {}


def _install_stubs(monkeypatch, calls, *, has_shapes, convert_result):
    img = Image.new("RGB", (100, 100), "white")

    def _rec(name, ret):
        def _fn(*a, **k):
            calls.setdefault(name, []).append((a, k))
            return ret
        return _fn

    # source detection + conversion (the code under test)
    monkeypatch.setattr(pipeline, "docx_has_renderable_shapes",
                        lambda b: has_shapes)
    monkeypatch.setattr(pipeline, "docx_to_pdf",
                        _rec("docx_to_pdf", convert_result))

    # page renderers — each records that it ran
    monkeypatch.setattr(pipeline, "pdf_to_images",
                        _rec("pdf_to_images", [PageImage(1, img)]))
    monkeypatch.setattr(pipeline, "docx_to_images",
                        _rec("docx_to_images", ([PageImage(1, img)], "")))
    monkeypatch.setattr(pipeline, "image_to_pages",
                        _rec("image_to_pages", [PageImage(1, img)]))

    monkeypatch.setattr(pipeline, "split_two_column_pages",
                        lambda pages: (pages, {1: {"src_page": 1, "x0": 0.0, "x1": 1.0}}))
    monkeypatch.setattr(pipeline, "compute_page_infos",
                        _rec("compute_page_infos", None))

    monkeypatch.setattr(pipeline, "AIAnalyzer", _FakeAnalyzer)
    monkeypatch.setattr(pipeline, "summarize_sections",
                        lambda qs: [{"section": 1, "max": 1}])
    monkeypatch.setattr(pipeline, "sections_confident", lambda s: False)
    monkeypatch.setattr(pipeline, "collapse_sections", lambda qs: qs)

    # figure attachment — record the pdf_bytes arg (3rd positional)
    def _attach_imgs(questions, page_images, pdf_bytes=None, *a, **k):
        calls.setdefault("attach_images_to_questions", []).append(pdf_bytes)
        return questions
    monkeypatch.setattr(pipeline, "attach_images_to_questions", _attach_imgs)
    monkeypatch.setattr(pipeline, "attach_docx_inline_images",
                        _rec("attach_docx_inline_images", 0))

    # cheap no-ops for the rest of the pipeline
    monkeypatch.setattr(pipeline, "recover_pdf_option_labels", lambda *a: None)
    monkeypatch.setattr(pipeline, "flag_mixed_case_labels", lambda *a: None)
    monkeypatch.setattr(pipeline, "restore_list_markers", lambda *a: None)
    monkeypatch.setattr(pipeline, "find_unanswerable", lambda qs: [])
    monkeypatch.setattr(pipeline, "find_near_duplicates", lambda qs: [])
    monkeypatch.setattr(pipeline, "flag_suspicious_questions", lambda qs: [])
    monkeypatch.setattr(pipeline, "find_siblings", lambda qs: [])
    monkeypatch.setattr(pipeline, "save_debug_crops", lambda *a: None)
    monkeypatch.setattr(pipeline, "prune_debug_crops", lambda *a, **k: 0)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(pipeline, "_set_project_status", _noop)
    monkeypatch.setattr(pipeline, "persist_questions", _noop)


async def test_shaped_docx_is_converted_and_uses_pdf_crop(monkeypatch):
    calls = {}
    _install_stubs(monkeypatch, calls, has_shapes=True, convert_result=b"%PDF-1.4 x")
    res = await pipeline.process_file(b"PK-docx", "docx", "pid")

    assert res.status == "ok"
    assert "docx_to_pdf" in calls                      # conversion attempted
    assert "pdf_to_images" in calls                    # PDF branch taken
    assert "docx_to_images" not in calls               # NOT the text render
    assert "attach_docx_inline_images" not in calls    # PDF crop path, not pairing
    # the crop received the converted PDF bytes
    assert calls["attach_images_to_questions"] == [b"%PDF-1.4 x"]


async def test_shapefree_docx_bypasses_conversion_byte_identical(monkeypatch):
    calls = {}
    _install_stubs(monkeypatch, calls, has_shapes=False, convert_result=b"%PDF")
    res = await pipeline.process_file(b"PK-docx-plain", "docx", "pid")

    assert res.status == "ok"
    assert "docx_to_pdf" not in calls                  # never converted
    assert "pdf_to_images" not in calls
    # rendered by docx_to_images with the ORIGINAL bytes → byte-identical
    assert calls["docx_to_images"][0][0][0] == b"PK-docx-plain"
    assert "attach_docx_inline_images" in calls        # normal DOCX image path
    assert calls["attach_images_to_questions"] == [None]  # no pdf → no crop


async def test_conversion_failure_falls_back_to_text_render(monkeypatch):
    calls = {}
    _install_stubs(monkeypatch, calls, has_shapes=True, convert_result=None)
    res = await pipeline.process_file(b"PK-docx-broken", "docx", "pid")

    assert res.status == "ok"
    assert "docx_to_pdf" in calls                      # attempted
    assert "pdf_to_images" not in calls                # conversion failed
    # fell back to the text render with the original bytes — visible, not a crash
    assert calls["docx_to_images"][0][0][0] == b"PK-docx-broken"
    assert "attach_docx_inline_images" in calls
