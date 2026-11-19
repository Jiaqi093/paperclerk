from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from paperclerk.models import PAGE_SEPARATOR, Page

__all__ = ["PAGE_SEPARATOR", "Page", "ParsedDocument", "Parser"]


@dataclass(frozen=True)
class ParsedDocument:
    mime_type: str
    # In order: pages[0] is page 1, so answers can cite pages. Formats without fixed pages
    # (plain text, DOCX, images) are one page.
    pages: tuple[Page, ...]

    @property
    def text(self) -> str:
        return PAGE_SEPARATOR.join(p.text for p in self.pages)

    @property
    def needs_vision(self) -> bool:
        return any(p.needs_vision for p in self.pages)


class Parser(Protocol):
    suffixes: frozenset[str]

    def parse(self, path: Path) -> ParsedDocument: ...
