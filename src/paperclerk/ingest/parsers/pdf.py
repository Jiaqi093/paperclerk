from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from paperclerk.ingest.parsers.base import Page, ParsedDocument, ParseError

log = logging.getLogger(__name__)

# A page with fewer visible characters than this has no usable text layer: a scan.
MIN_CHARS_PER_PAGE = 30
# A page mostly covered by images with only a little text is a scan too, e.g. a phone scan
# whose app stamped one line of text on top ("Scanned by CamScanner 2026-10-24 10:31").
# Starting points; tune them on the F6 eval set.
IMAGE_COVERAGE = 0.5  # fraction of the page area covered by images
MAX_CHARS_ON_IMAGE_PAGE = 200
# How deep to follow forms nested in forms when looking for images.
MAX_FORM_DEPTH = 8

# PDFium is process-global and not thread-safe: one PDF at a time, whoever is asking.
_pdfium_lock = threading.Lock()


class PdfParser:
    """Text per page, with each page flagged for the vision model when it looks scanned.

    Uses pypdfium2 (PDFium, Apache-2.0/BSD) rather than PyMuPDF (AGPL), so the project stays
    MIT. A page PDFium can't read becomes an empty page flagged for vision rather than failing
    the whole document.
    """

    suffixes = frozenset({".pdf"})
    version = 1

    def parse(self, data: bytes, path: Path) -> ParsedDocument:
        with _pdfium_lock:
            try:
                pdf = pdfium.PdfDocument(data)
            except pdfium.PdfiumError as e:
                if getattr(e, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD:
                    raise ParseError("password protected") from e
                # Also covers a valid PDF with zero pages: PDFium rejects those on open.
                raise ParseError("damaged or not a valid PDF") from e
            try:
                pages = tuple(_read_page_or_blank(pdf, i, path) for i in range(len(pdf)))
            finally:
                pdf.close()
        return ParsedDocument(mime_type="application/pdf", pages=pages)


def _read_page_or_blank(pdf: pdfium.PdfDocument, index: int, path: Path) -> Page:
    try:
        return _read_page(pdf[index])
    except pdfium.PdfiumError:
        # One broken page (bad object reference, ...) shouldn't lose the others. Keep its place
        # so page numbers stay right, and let the vision model look at it.
        log.warning("can't read page %d of %s", index + 1, path, exc_info=True)
        return Page("", needs_vision=True)


def _read_page(page: pdfium.PdfPage) -> Page:
    textpage = page.get_textpage()
    try:
        # All text on the page, not only what lies inside its box: some producers put content
        # outside it. PDFium ends lines with \r\n; keep \n only.
        text = textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n").strip()
    finally:
        textpage.close()
    return Page(text, needs_vision=_looks_scanned(page, text))


def _looks_scanned(page: pdfium.PdfPage, text: str) -> bool:
    chars = sum(1 for ch in text if not ch.isspace())
    if chars < MIN_CHARS_PER_PAGE:
        return True
    return chars <= MAX_CHARS_ON_IMAGE_PAGE and _image_coverage(page) >= IMAGE_COVERAGE


def _image_coverage(page: pdfium.PdfPage) -> float:
    """Roughly how much of the page images cover (0-1). Overlapping images count twice,
    so the result is capped at 1."""
    left, bottom, right, top = page.get_cropbox()
    area = (right - left) * (top - bottom)
    if area <= 0:
        return 0.0
    covered = 0.0
    for x0, y0, x1, y1 in _image_rects(page):
        w = min(x1, right) - max(x0, left)
        h = min(y1, top) - max(y0, bottom)
        if w > 0 and h > 0:
            covered += w * h
    return min(covered / area, 1.0)


def _image_rects(
    page: pdfium.PdfPage,
    form: pdfium.PdfObject | None = None,
    to_page: tuple[pdfium.PdfMatrix, ...] = (),
    depth: int = 0,
) -> Iterator[tuple[float, float, float, float]]:
    """Bounds of every image on the page, in page coordinates.

    Objects inside a form (a reusable block of content, common in stamped or merged PDFs)
    report bounds in the form's own space, so each enclosing form's matrix is applied, the
    innermost first.
    """
    for obj in page.get_objects(max_depth=1, form=form):  # this level only
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            rect = obj.get_bounds()
            for matrix in to_page:
                rect = matrix.on_rect(*rect)
            yield rect
        elif obj.type == pdfium_c.FPDF_PAGEOBJ_FORM and depth < MAX_FORM_DEPTH:
            yield from _image_rects(page, obj, (obj.get_matrix(), *to_page), depth + 1)
