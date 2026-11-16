from __future__ import annotations

import mimetypes
from pathlib import Path

from paperclerk.ingest.parsers.base import ParsedDocument


class ImageParser:
    """Images carry no text layer: mark them for the vision model in F2."""

    suffixes = frozenset({".png", ".jpg", ".jpeg", ".webp"})

    def parse(self, path: Path) -> ParsedDocument:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return ParsedDocument(text="", mime_type=mime, pages=[], needs_vision=True)
