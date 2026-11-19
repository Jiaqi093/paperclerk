from __future__ import annotations

import mimetypes
from pathlib import Path

from paperclerk.ingest.parsers.base import Page, ParsedDocument


class ImageParser:
    """Images carry no text layer: mark them for the vision model in F2."""

    suffixes = frozenset({".png", ".jpg", ".jpeg", ".webp"})

    def parse(self, data: bytes, path: Path) -> ParsedDocument:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return ParsedDocument(mime_type=mime, pages=(Page("", needs_vision=True),))
