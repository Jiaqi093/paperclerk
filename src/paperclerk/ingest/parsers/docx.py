from __future__ import annotations

from pathlib import Path

from paperclerk.ingest.parsers.base import ParsedDocument


class DocxParser:
    suffixes = frozenset({".docx"})

    def parse(self, path: Path) -> ParsedDocument:
        # TODO(F1): read paragraphs and tables with python-docx.
        raise NotImplementedError("DOCX parsing not implemented yet (F1)")
