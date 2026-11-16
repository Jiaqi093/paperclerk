from __future__ import annotations

from pathlib import Path

from paperclerk.ingest.parsers.base import ParsedDocument


class DocxParser:
    suffixes = frozenset({".docx"})

    def parse(self, path: Path) -> ParsedDocument:
        # TODO(week 1): read paragraphs and tables with python-docx.
        raise NotImplementedError("DOCX parsing lands in week 1 (F1)")
