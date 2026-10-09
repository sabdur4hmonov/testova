"""Archive limits for uploaded DOCX files (AUDIT F08).

A DOCX is a ZIP archive. Before this check every DOCX reader trusted it
blindly: the shape probe read `word/document.xml` whole, python-docx loads
every part into memory, and LibreOffice expands it again. A 77 KB upload whose
document.xml is 40 MiB of whitespace made one probe allocate ~89 MB, and a
20 MB upload of the same shape expands to gigabytes — the bot container would
be OOM-killed for every teacher at once.

validate_docx() runs BEFORE any parser or renderer sees the bytes and rejects:
  * too many entries, unsafe or duplicate entry names, symlinks, encrypted or
    non-standard compression;
  * a declared or ACTUAL expanded size over the per-part / total budget, or a
    suspicious compression ratio. Entries are really decompressed in bounded
    chunks, because ZIP headers can lie;
  * a DTD in any XML part (OOXML never has one; it is the billion-laughs and
    external-entity vector for the XML parsers downstream);
  * external relationships that a renderer would FETCH (linked images,
    OLE objects, frames, sub-documents). Hyperlinks and the attached template
    path are external too but are never fetched, so they stay allowed.

Limits are module constants so tests can shrink them.
"""
from __future__ import annotations

import io
import stat
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

# Telegram's bot download cap is 20 MB, so a larger blob never came from a
# teacher's upload.
MAX_COMPRESSED_BYTES = 20 * 1024 * 1024
# Real DOCX files carry tens of parts; a few hundred with many pictures.
MAX_ENTRIES = 2000
# XML parts become lxml trees several times their size, so they get a tighter
# budget than media parts (pictures are already compressed, ratio ~1).
MAX_XML_PART_BYTES = 16 * 1024 * 1024
MAX_PART_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
# A ratio check only matters for parts big enough to hurt; small XML parts
# (styles, settings) routinely compress 20-100x.
MAX_RATIO = 200
RATIO_MIN_BYTES = 1024 * 1024

_CHUNK = 64 * 1024
_XML_SUFFIXES = (".xml", ".rels", ".vml")
# Scan the XML encodings downstream parsers recognize. ASCII-only scanning
# misses UTF-16/32 declarations, whose characters contain zero bytes.
_DTD_MARKERS = tuple(
    marker.encode(encoding)
    for marker in ("<!DOCTYPE", "<!ENTITY")
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
)
# Longest marker minus one: carried between chunks so a marker split across a
# chunk boundary is still found.
_MARKER_OVERLAP = max(len(m) for m in _DTD_MARKERS) - 1

_RELS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
# External targets that are only ever stored, never fetched by python-docx or
# LibreOffice when opening/converting.
_SAFE_EXTERNAL_REL_TYPES = ("/hyperlink", "/attachedTemplate")


class InvalidDocx(ValueError):
    """The DOCX archive is malformed, oversized or unsafe to open."""


def _xml_part(name: str) -> bool:
    return name.lower().endswith(_XML_SUFFIXES)


def _check_entry_header(info: zipfile.ZipInfo, seen: set[str]) -> None:
    name = info.orig_filename
    parts = PurePosixPath(name).parts
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or ":" in name
        or name.startswith("/")
        or ".." in parts
    ):
        raise InvalidDocx("unsafe archive path")
    # Case-insensitive: Windows-built readers and LibreOffice may resolve a
    # duplicate differently than python-docx, so the two would see different
    # documents.
    key = name.casefold()
    if key in seen:
        raise InvalidDocx("duplicate archive entry")
    seen.add(key)

    if stat.S_ISLNK(info.external_attr >> 16):
        raise InvalidDocx("symbolic link in archive")
    if info.flag_bits & 0x1:
        raise InvalidDocx("encrypted archive entry")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise InvalidDocx("unsupported archive compression")

    limit = MAX_XML_PART_BYTES if _xml_part(name) else MAX_PART_BYTES
    if info.file_size > limit:
        raise InvalidDocx("archive part too large")
    if (
        info.file_size > RATIO_MIN_BYTES
        and info.file_size > MAX_RATIO * max(1, info.compress_size)
    ):
        raise InvalidDocx("archive compression ratio too high")


def _read_entry(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int
) -> tuple[int, bytes | None]:
    """Decompress one entry in bounded chunks; return (bytes read, content).

    Content is kept only for .rels parts (needed for the external-target
    check); everything else is streamed and discarded.
    """
    name = info.orig_filename
    limit = MAX_XML_PART_BYTES if _xml_part(name) else MAX_PART_BYTES
    scan_dtd = _xml_part(name)
    keep = name.lower().endswith(".rels")
    kept: list[bytes] = []
    tail = b""
    read = 0
    with archive.open(info) as stream:
        while chunk := stream.read(_CHUNK):
            read += len(chunk)
            if read > limit:
                raise InvalidDocx("archive part too large")
            if read > budget:
                raise InvalidDocx("archive expands too much")
            if scan_dtd:
                window = tail + chunk
                if any(marker in window for marker in _DTD_MARKERS):
                    raise InvalidDocx("DTD in XML part")
                tail = window[-_MARKER_OVERLAP:]
            if keep:
                kept.append(chunk)
    # zipfile stops at the DECLARED size and checks the CRC there, so a header
    # that understates the size fails the CRC (BadZipFile) before we get here;
    # this catches the remaining mismatch.
    if read != info.file_size:
        raise InvalidDocx("archive size mismatch")
    return read, (b"".join(kept) if keep else None)


def _check_external_targets(rels_xml: bytes) -> None:
    try:
        root = ElementTree.fromstring(rels_xml)
    except ElementTree.ParseError as e:
        raise InvalidDocx("malformed relationships part") from e
    for rel in root.iter(f"{_RELS_NS}Relationship"):
        if rel.get("TargetMode") != "External":
            continue
        rel_type = rel.get("Type", "")
        if not rel_type.endswith(_SAFE_EXTERNAL_REL_TYPES):
            raise InvalidDocx("external resource reference")


def validate_docx(blob: bytes) -> None:
    """Raise InvalidDocx unless `blob` is a DOCX archive safe to parse."""
    if len(blob) > MAX_COMPRESSED_BYTES:
        raise InvalidDocx("file too large")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise InvalidDocx("too many archive entries")

            seen: set[str] = set()
            declared_total = 0
            for info in entries:
                _check_entry_header(info, seen)
                declared_total += info.file_size
                if declared_total > MAX_TOTAL_BYTES:
                    raise InvalidDocx("archive expands too much")

            total = 0
            for info in entries:
                if info.is_dir():
                    continue
                read, rels = _read_entry(archive, info, MAX_TOTAL_BYTES - total)
                total += read
                if rels is not None:
                    _check_external_targets(rels)
    except InvalidDocx:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, ValueError, RuntimeError) as e:
        # RuntimeError: zipfile's "encrypted, password required" path.
        raise InvalidDocx("malformed archive") from e
