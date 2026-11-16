"""F1 — walk watched folders and find files that are new or changed since the last scan."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from paperclerk.ingest.parsers import SUPPORTED_SUFFIXES

CHUNK = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size_bytes: int
    mtime: float


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def iter_candidate_files(root: Path) -> Iterator[FileInfo]:
    """Yield supported, non-hidden files under root."""
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        st = p.stat()
        yield FileInfo(path=p, size_bytes=st.st_size, mtime=st.st_mtime)


def iter_changed_files(
    root: Path, is_up_to_date: Callable[[str, float], bool]
) -> Iterator[FileInfo]:
    """Yield files the database doesn't have up to date (new, changed, or not parsed yet)."""
    for info in iter_candidate_files(root):
        if not is_up_to_date(str(info.path), info.mtime):
            yield info
