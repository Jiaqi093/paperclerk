from __future__ import annotations

from pathlib import Path

from paperclerk.ingest.parsers.base import ParsedDocument

# A page with fewer characters than this is treated as scanned (sent to the vision model).
MIN_CHARS_PER_PAGE = 30


class PdfParser:
    suffixes = frozenset({".pdf"})

    def parse(self, path: Path) -> ParsedDocument:
        # TODO(F1): extract per-page text with PyMuPDF (import pymupdf); a page with fewer than
        # MIN_CHARS_PER_PAGE characters gets needs_vision=True.
        raise NotImplementedError("PDF parsing not implemented yet (F1)")
