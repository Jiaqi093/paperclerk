from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ParsedDocument:
    text: str
    mime_type: str
    # Page-level text (1-based page = index + 1) so answers can cite pages.
    pages: list[str] = field(default_factory=list)
    # True when there is little or no text layer (scans, photos): send to a vision model.
    needs_vision: bool = False


class Parser(Protocol):
    suffixes: frozenset[str]

    def parse(self, path: Path) -> ParsedDocument: ...
