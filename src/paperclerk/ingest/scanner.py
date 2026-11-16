"""F1 — walk watched folders and find files that are new or changed since the last scan."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
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


def is_candidate(root: Path, path: Path) -> bool:
    """Whether a walk of `root` considers `path`: a supported type, not hidden under root."""
    if path.suffix.lower() not in SUPPORTED_SUFFIXES or not path.is_relative_to(root):
        return False
    return not any(part.startswith(".") for part in path.relative_to(root).parts)


def iter_candidate_files(root: Path) -> Iterator[FileInfo]:
    """Yield supported, non-hidden files under root.

    A file that vanishes or can't be stat'ed mid-walk (a download being renamed, a permission
    error) is skipped rather than aborting the walk.
    """
    for p in sorted(root.rglob("*")):
        if not is_candidate(root, p):
            continue
        try:
            if not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        yield FileInfo(path=p, size_bytes=st.st_size, mtime=st.st_mtime)
