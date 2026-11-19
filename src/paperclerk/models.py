"""Plain data types shared across layers (parsers produce them, the db layer stores them)."""

from __future__ import annotations

from dataclasses import dataclass

# Joins page texts into a document's full text.
PAGE_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class Page:
    text: str
    # Little or no text layer (a scanned page, a photo): send this page to a vision model.
    needs_vision: bool = False
