"""Encoded XML declarations are rejected before opening a DOCX."""
import io
import zipfile
import pytest
from app.services.docx_limits import InvalidDocx, validate_docx

@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
@pytest.mark.parametrize("small_chunks", [False, True])
def test_encoded_xml_dtd_is_rejected(monkeypatch, encoding, small_chunks):
    from app.services import docx_limits
    if small_chunks:
        monkeypatch.setattr(docx_limits, "_CHUNK", 7)
    # Small internal entity; neither resource exhaustion nor external access.
    xml = '<?xml version="1.0"?><!DOCTYPE document [<!ENTITY x "synthetic">]><document>&x;</document>'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml.encode(encoding))
    with pytest.raises(InvalidDocx, match="DTD"):
        validate_docx(archive.getvalue())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_encoded_xml_without_dtd_passes(encoding):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", '<document>synthetic</document>'.encode(encoding))
    validate_docx(archive.getvalue())
