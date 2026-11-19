from __future__ import annotations

import collections
import itertools
import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from paperclerk.ingest.parsers.base import Page, ParsedDocument, ParseError

log = logging.getLogger(__name__)

# A page with fewer visible characters than this has no usable text layer. It needs vision
# if something else is drawn on it: a picture covering at least MIN_IMAGE_COVERAGE of it, or
# text converted to vector outlines (some generators do this). Outlines are recognised by
# rendering the page small and counting lines of ink; one path can hold a whole line of
# text, so counting paths alone misses them. A nearly empty page, e.g. one heading in a
# frame, isn't worth a vision call.
MIN_CHARS_PER_PAGE = 30
MIN_IMAGE_COVERAGE = 0.05  # a card-sized scan on A4 covers ~7.5%
MIN_VECTOR_PATHS = 20  # fewer shapes than this (a frame, a rule) can't be a page of text
MIN_INK_LINES = 10
INK_ROW_SHARE = 0.02  # share of a pixel row that must be ink for it to be part of a line
_INK_SCALE = 0.5  # 72 dpi * 0.5: an A4 page renders to about 300x420 pixels
_INK_MAX_PIXELS = 1000  # longest side of the render, so huge pages (A0 plans) stay cheap
_INK_CONTRAST = 40  # grey levels from the background that count as ink (antialiased strokes)
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
    # 2: nearly empty pages aren't sent to vision; U+FFFE read as "-"
    # 3: text drawn as outlines found by lines of ink, not by counting paths
    version = 3

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
        # PDFium joins a word hyphenated across a line break into one, marking the hyphen as
        # U+FFFE ("Carrier-Imposed", but also "inter-national"). Keep it as "-": right for
        # compounds; split words stay readable to a model, though not to exact search.
        text = text.replace("\ufffe", "-")
    finally:
        textpage.close()
    return Page(text, needs_vision=_looks_scanned(page, text))


def _looks_scanned(page: pdfium.PdfPage, text: str) -> bool:
    chars = sum(1 for ch in text if not ch.isspace())
    if chars < MIN_CHARS_PER_PAGE:
        if _image_coverage(page) >= MIN_IMAGE_COVERAGE:
            return True
        paths = page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_PATH], max_depth=MAX_FORM_DEPTH)
        if sum(1 for _ in itertools.islice(paths, MIN_VECTOR_PATHS)) < MIN_VECTOR_PATHS:
            return False
        return _ink_lines(page) >= MIN_INK_LINES
    return chars <= MAX_CHARS_ON_IMAGE_PAGE and _image_coverage(page) >= IMAGE_COVERAGE


def _ink_lines(page: pdfium.PdfPage) -> int:
    """Roughly how many lines of text are drawn on the page, from a small grayscale render.

    Ink is any pixel clearly different from the page background (the most common shade), so
    grey text, thin antialiased strokes and light-on-dark pages all count. A row is part of a
    line when INK_ROW_SHARE of it is ink, so a frame or a rule running down the page doesn't
    merge every line into one. Bands are counted across rows and across columns and the larger
    count wins, so text drawn sideways (a rotated page) is found too.
    """
    width_pt, height_pt = page.get_size()
    scale = min(_INK_SCALE, _INK_MAX_PIXELS / max(width_pt, height_pt, 1))
    bitmap = page.render(scale=scale, grayscale=True)
    try:
        buf, width, height, stride = (
            bytes(bitmap.buffer),
            bitmap.width,
            bitmap.height,
            bitmap.stride,
        )
    finally:
        bitmap.close()
    if not width or not height:
        return 0
    background = collections.Counter(buf[: stride * height]).most_common(1)[0][0]
    is_ink = bytes(1 if abs(v - background) > _INK_CONTRAST else 0 for v in range(256))
    ink = buf.translate(is_ink)  # 1 per ink pixel, same layout as the bitmap
    row_ink = [sum(ink[y * stride : y * stride + width]) for y in range(height)]
    col_ink = [sum(ink[x : stride * height : stride]) for x in range(width)]
    return max(_bands(row_ink, width), _bands(col_ink, height))


def _bands(ink_per_line: list[int], length: int) -> int:
    threshold = max(1, int(length * INK_ROW_SHARE))
    bands, inside = 0, False
    for n in ink_per_line:
        dark = n >= threshold
        if dark and not inside:
            bands += 1
        inside = dark
    return bands


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
