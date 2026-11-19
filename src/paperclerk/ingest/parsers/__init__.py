"""Parser registry: one parser per file type, chosen by suffix."""

from __future__ import annotations

from pathlib import Path

from paperclerk.ingest.parsers.base import Page, ParsedDocument, Parser
from paperclerk.ingest.parsers.docx import DocxParser
from paperclerk.ingest.parsers.image import ImageParser
from paperclerk.ingest.parsers.pdf import PdfParser
from paperclerk.ingest.parsers.text import TextParser

_PARSERS: list[Parser] = [PdfParser(), DocxParser(), ImageParser(), TextParser()]

SUPPORTED_SUFFIXES: frozenset[str] = frozenset(s for p in _PARSERS for s in p.suffixes)


def parser_for(path: Path) -> Parser | None:
    suffix = path.suffix.lower()
    return next((p for p in _PARSERS if suffix in p.suffixes), None)


__all__ = ["Page", "ParsedDocument", "Parser", "SUPPORTED_SUFFIXES", "parser_for"]
