from __future__ import annotations

from pathlib import Path

from paperclerk.ingest.parsers.base import ParsedDocument


class TextParser:
    suffixes = frozenset({".txt", ".md", ".csv"})

    def parse(self, path: Path) -> ParsedDocument:
        text = path.read_text(encoding="utf-8", errors="replace")
        return ParsedDocument(text=text, mime_type="text/plain", pages=[text])
