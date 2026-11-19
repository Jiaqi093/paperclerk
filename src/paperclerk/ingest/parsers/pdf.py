from __future__ import annotations

from pathlib import Path

from paperclerk.ingest.parsers.base import ParsedDocument

# Below this many characters per page we treat the PDF as a scan.
MIN_CHARS_PER_PAGE = 30


class PdfParser:
    suffixes = frozenset({".pdf"})

    def parse(self, path: Path) -> ParsedDocument:
        # TODO(F1): extract per-page text with PyMuPDF (import fitz),
        # set needs_vision=True when avg chars/page < MIN_CHARS_PER_PAGE.
        raise NotImplementedError("PDF parsing not implemented yet (F1)")
