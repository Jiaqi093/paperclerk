from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path

import docx
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from paperclerk.ingest.parsers.base import Page, ParsedDocument, ParseError

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
IMAGE_TAGS = (
    "{http://schemas.openxmlformats.org/drawingml/2006/main}blip",  # modern pictures
    "{urn:schemas-microsoft-com:vml}imagedata",  # legacy (VML) pictures
)
# Content that isn't part of the document as it reads now: tracked deletions, the source of
# a tracked move (its text also appears at the destination), field codes.
SKIPPED = frozenset({W + "del", W + "moveFrom", W + "delText", W + "instrText"})

# A body with an image and at most this many characters is treated as a pasted scan or
# photo: F2 sends its embedded images to the vision model (a DOCX can't be rendered as pages
# without LibreOffice). Same limit as a PDF page mostly covered by images.
MAX_CHARS_WITH_IMAGE = 200
# python-docx inflates every part up front; refuse archives that would inflate past this
# (a "zip bomb" can be a few KB on disk). Photos are already compressed, so real documents
# stay close to their file size.
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
# Cap on how many columns one merged cell may claim (w:gridSpan), against malformed files.
MAX_GRID_SPAN = 64

# Encrypted (password-protected) Office files aren't zip files but OLE containers holding an
# "EncryptedPackage" stream; old binary .doc files are OLE containers too.
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DocxParser:
    """Body text with tables as Markdown, plus header and footer text, as one page.

    Reads the XML directly rather than through python-docx's paragraph API, so that text boxes,
    content controls and tracked insertions are included and tracked deletions are not.
    Hidden text is skipped. Footnotes and comments aren't read.
    """

    suffixes = frozenset({".docx"})
    version = 1

    def parse(self, data: bytes, path: Path) -> ParsedDocument:
        if data.startswith(OLE_MAGIC):
            if ENCRYPTED_PACKAGE in data:
                raise ParseError("password protected")
            raise ParseError("old Word format (.doc) saved as .docx")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                unzipped = sum(i.file_size for i in z.infolist())
        except zipfile.BadZipFile as e:
            raise ParseError("damaged or not a valid Word document") from e
        if unzipped > MAX_UNZIPPED_BYTES:
            raise ParseError("too large to read safely")
        try:
            document = docx.Document(io.BytesIO(data))
        except Exception as e:  # missing parts, malformed XML
            raise ParseError("damaged or not a valid Word document") from e

        body = document.element.body
        body_text = "\n".join(t for t in _blocks(body) if t)
        headers, footers = _header_footer_texts(document)
        text = "\n\n".join(t for t in (*headers, body_text, *footers) if t)
        chars = sum(1 for ch in body_text if not ch.isspace())
        needs_vision = _has_image(body) and chars <= MAX_CHARS_WITH_IMAGE
        return ParsedDocument(mime_type=DOCX_MIME, pages=(Page(text, needs_vision),))


def _find(element, tags: tuple[str, ...]) -> Iterator:
    """Elements with one of `tags` under `element`, in order, looking through wrappers
    (content controls, custom XML, tracked insertions) but not inside a match."""
    for child in element:
        if child.tag in SKIPPED:
            continue
        if child.tag in tags:
            yield child
        else:
            yield from _find(child, tags)


def _blocks(container, *, inline_tables: bool = False) -> Iterator[str]:
    """Text of each paragraph and table in `container`, in document order."""
    for el in _find(container, (W + "p", W + "tbl")):
        if el.tag == W + "p":
            yield _paragraph_text(el)
        elif inline_tables:  # a table nested in a table cell: keep it on one line
            yield " ".join(c for row in _table_rows(el) for c in row if c)
        else:
            yield _table_markdown(el)


def _paragraph_text(p) -> str:
    parts: list[str] = []
    _collect(p, parts)
    return "".join(parts).strip()


def _collect(element, parts: list[str]) -> None:
    for child in element:
        tag = child.tag
        if tag in SKIPPED:
            continue
        if tag == W + "t":
            parts.append(child.text or "")
        elif tag in (W + "tab", W + "ptab"):
            parts.append("\t")
        elif tag in (W + "br", W + "cr"):
            parts.append("\n")
        elif tag == W + "noBreakHyphen":
            parts.append("-")
        elif tag == W + "r" and _hidden(child):
            continue
        elif tag == W + "p":  # a paragraph inside a text box anchored in this paragraph
            parts.append("\n")
            _collect(child, parts)
            parts.append("\n")
        elif tag == MC + "AlternateContent":
            _collect_alternate(child, parts)
        else:
            _collect(child, parts)


def _collect_alternate(alt, parts: list[str]) -> None:
    """Word writes some content (text boxes) twice: a modern mc:Choice and a legacy
    mc:Fallback copy. Read the first that has text."""
    for option in alt:
        found: list[str] = []
        _collect(option, found)
        if "".join(found).strip():
            parts.extend(found)
            return


def _hidden(run) -> bool:
    vanish = run.find(f"{W}rPr/{W}vanish")
    return vanish is not None and vanish.get(W + "val", "true") not in ("false", "0", "off")


def _table_rows(tbl) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in _find(tbl, (W + "tr",)):
        row: list[str] = []
        for tc in _find(tr, (W + "tc",)):
            text = " ".join(t for t in _blocks(tc, inline_tables=True) if t)
            row.append(text.replace("\n", " "))
            # A cell merged across columns appears once; pad so later cells stay aligned.
            row.extend([""] * (_grid_span(tc) - 1))
        rows.append(row)
    return rows


def _grid_span(tc) -> int:
    span = tc.find(f"{W}tcPr/{W}gridSpan")
    if span is None:
        return 1
    try:
        return max(1, min(int(span.get(W + "val", "1")), MAX_GRID_SPAN))
    except ValueError:
        return 1


def _table_markdown(tbl) -> str:
    rows = [[c.replace("|", "\\|") for c in row] for row in _table_rows(tbl)]
    if not rows:
        return ""
    width = max(len(r) for r in rows) or 1
    lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
    lines.insert(1, "|" + " --- |" * width)
    return "\n".join(lines)


def _header_footer_texts(document) -> tuple[list[str], list[str]]:
    """Distinct header and footer texts (sections often repeat the same one), in part order
    (header2 before header10). First-page and even-page variants are included."""
    headers: dict[str, None] = {}
    footers: dict[str, None] = {}
    rels = [
        r
        for r in document.part.rels.values()
        if not r.is_external and r.reltype in (RT.HEADER, RT.FOOTER)
    ]
    for rel in sorted(rels, key=lambda r: _natural_key(str(r.target_ref))):
        text = "\n".join(t for t in _blocks(rel.target_part.element) if t)
        if text:
            (headers if rel.reltype == RT.HEADER else footers)[text] = None
    return list(headers), list(footers)


def _natural_key(s: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", s)]


def _has_image(body) -> bool:
    return any(next(body.iter(tag), None) is not None for tag in IMAGE_TAGS)
