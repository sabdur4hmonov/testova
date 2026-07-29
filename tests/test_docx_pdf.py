"""Defect 3: DOCX shape detection + headless-LibreOffice conversion.

Pure unit tests — no real `soffice` needed. The subprocess is monkeypatched so
we can prove every branch, especially the fallback ones (missing binary,
timeout, non-zero exit), WITHOUT depending on LibreOffice being installed. The
real end-to-end render is proven separately, in Docker, against File 1.
"""
import io
import subprocess
import zipfile

import pytest

from app.services import file_processor as fp


# ── Minimal in-memory DOCX (no sample files) ──────────────────────────────────

def _docx_bytes(document_xml: str) -> bytes:
    """A minimal .docx zip carrying the given word/document.xml body."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", document_xml)
    return buf.getvalue()


_PLAIN = _docx_bytes(
    '<w:document xmlns:w="x"><w:body>'
    '<w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>'
)
_VML = _docx_bytes(
    '<w:document xmlns:w="x"><w:body><w:p><w:pict>'
    '<v:shape id="s1" style="width:10pt"/></w:pict></w:p></w:body></w:document>'
)
_OMML = _docx_bytes(
    '<w:document xmlns:w="x"><w:body><w:p>'
    '<m:oMath><m:r><m:t>a/b</m:t></m:r></m:oMath></w:p></w:body></w:document>'
)
# DrawingML AutoShape with NO VML fallback (Google Docs / LibreOffice encoding):
# a wps:wsp shape wrapped in a:graphic, no <v:shape>/<w:pict> anywhere.
_DRAWINGML_SHAPE = _docx_bytes(
    '<w:document xmlns:w="x"><w:body><w:p><w:r><w:drawing>'
    '<wp:inline xmlns:wp="x"><a:graphic xmlns:a="x"><a:graphicData>'
    '<wps:wsp xmlns:wps="x"><wps:spPr/></wps:wsp>'
    '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    '</w:body></w:document>'
)
_DRAWINGML_GROUP = _docx_bytes(
    '<w:document xmlns:w="x"><w:body><w:p><w:r><w:drawing>'
    '<a:graphic xmlns:a="x"><a:graphicData><wpg:wgp xmlns:wpg="x"/></a:graphicData>'
    '</a:graphic></w:drawing></w:r></w:p></w:body></w:document>'
)
# A plain INLINE RASTER photo: a:graphic + pic:pic + a:blip, but NO vector
# shape (wsp/wgp/wpc). This is the case that must STAY on the python-docx path —
# it is why a:graphic must never be a shape marker.
_RASTER_ONLY = _docx_bytes(
    '<w:document xmlns:w="x"><w:body><w:p><w:r><w:drawing>'
    '<wp:inline xmlns:wp="x"><a:graphic xmlns:a="x"><a:graphicData>'
    '<pic:pic xmlns:pic="x"><pic:blipFill><a:blip r:embed="rId1"/></pic:blipFill>'
    '<pic:spPr><a:prstGeom prst="rect"/></pic:spPr></pic:pic>'
    '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    '</w:body></w:document>'
)


def _docx_two_parts(document_xml: str, header_xml: str) -> bytes:
    """A .docx whose header carries a shape but whose body is plain."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", document_xml)
        z.writestr("word/header1.xml", header_xml)
    return buf.getvalue()


_SHAPE_IN_HEADER_ONLY = _docx_two_parts(
    '<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Plain body</w:t></w:r>'
    '</w:p></w:body></w:document>',
    '<w:hdr xmlns:w="x"><wps:wsp xmlns:wps="x"/></w:hdr>',
)


# ── docx_has_renderable_shapes ────────────────────────────────────────────────

def test_shapes_detected_vml():
    assert fp.docx_has_renderable_shapes(_VML) is True


def test_shapes_detected_omml():
    assert fp.docx_has_renderable_shapes(_OMML) is True


def test_shapes_detected_drawingml_autoshape():
    # wps:wsp with no VML fallback — the blind spot this change closes.
    assert fp.docx_has_renderable_shapes(_DRAWINGML_SHAPE) is True


def test_shapes_detected_drawingml_group():
    assert fp.docx_has_renderable_shapes(_DRAWINGML_GROUP) is True


def test_no_shapes_plain_docx():
    assert fp.docx_has_renderable_shapes(_PLAIN) is False


def test_raster_only_docx_is_not_a_shape():
    # a:graphic + pic:pic + a:blip but no wsp/wgp/wpc → must NOT convert.
    # Guards the working text+photo path (the reason a:graphic is excluded).
    assert fp.docx_has_renderable_shapes(_RASTER_ONLY) is False


def test_shape_in_header_does_not_trigger():
    # Detection is gated to the document body; a header-only shape is ignored.
    assert fp.docx_has_renderable_shapes(_SHAPE_IN_HEADER_ONLY) is False


def test_shape_probe_bad_bytes_is_false():
    # A corrupt / non-zip payload must not raise — keep the current path.
    assert fp.docx_has_renderable_shapes(b"not a zip") is False


# ── docx_to_pdf: the fallback branches (the ones we care most about) ───────────

def test_conversion_missing_binary_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("soffice")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert fp.docx_to_pdf(_VML) is None


def test_conversion_timeout_returns_none(monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=1)
    monkeypatch.setattr(subprocess, "run", _timeout)
    assert fp.docx_to_pdf(_VML, timeout=1) is None


def test_conversion_nonzero_exit_returns_none(monkeypatch):
    class _Proc:
        returncode = 1
        stderr = b"conversion error"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    assert fp.docx_to_pdf(_VML) is None


def test_conversion_success_returns_pdf_bytes(monkeypatch):
    # Simulate soffice writing in.pdf into --outdir and exiting 0.
    def _fake_run(cmd, **k):
        outdir = cmd[cmd.index("--outdir") + 1]
        (io.open(f"{outdir}/in.pdf", "wb")).write(b"%PDF-1.4 fake\n%%EOF")

        class _Proc:
            returncode = 0
            stderr = b""
        return _Proc()
    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = fp.docx_to_pdf(_VML)
    assert out is not None and out.startswith(b"%PDF")


def test_conversion_bad_output_returns_none(monkeypatch):
    # Exit 0 but the produced file is not a PDF → reject, fall back.
    def _fake_run(cmd, **k):
        outdir = cmd[cmd.index("--outdir") + 1]
        (io.open(f"{outdir}/in.pdf", "wb")).write(b"garbage")

        class _Proc:
            returncode = 0
            stderr = b""
        return _Proc()
    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert fp.docx_to_pdf(_VML) is None
