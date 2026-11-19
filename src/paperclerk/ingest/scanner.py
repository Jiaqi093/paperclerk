"""F1 — walk watched folders and find files that are new or changed since the last scan."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import logging
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from paperclerk.ingest.parsers import SUPPORTED_SUFFIXES

CHUNK = 1 << 20  # 1 MiB
# macOS st_flags bit for a file whose content lives only in the cloud (iCloud Drive with
# "Optimize Mac Storage", Dropbox, OneDrive, Google Drive: all use File Provider). Reading
# one downloads it, so scans leave these alone. Not in Python's stat module before 3.13.
SF_DATALESS = 0x40000000

# <sys/resource.h>: a thread with this I/O policy OFF gets EDEADLK instead of a download when
# it reads a dataless file. That closes the gap between checking SF_DATALESS and reading.
_IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
_IOPOL_SCOPE_THREAD = 1
_IOPOL_MATERIALIZE_DATALESS_FILES_OFF = 1

log = logging.getLogger(__name__)


@contextlib.contextmanager
def no_cloud_downloads() -> Iterator[None]:
    """On macOS, make reads in this thread fail (EDEADLK) rather than download cloud-only
    files. Elsewhere, or if the call fails, does nothing."""
    if sys.platform != "darwin":
        yield
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        policy = (_IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, _IOPOL_SCOPE_THREAD)
        before = libc.getiopolicy_np(*policy)
        ok = (
            before >= 0 and libc.setiopolicy_np(*policy, _IOPOL_MATERIALIZE_DATALESS_FILES_OFF) == 0
        )
    except (OSError, AttributeError):
        ok = False
    if not ok:
        log.warning("couldn't stop reads from downloading cloud-only files")
        yield
        return
    try:
        yield
    finally:
        libc.setiopolicy_np(*policy, before)


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size_bytes: int
    mtime: float
    cloud_only: bool = False  # content not downloaded (see SF_DATALESS): don't read it


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


def iter_candidate_files(root: Path, unreadable: list[Path] | None = None) -> Iterator[FileInfo]:
    """Yield supported, non-hidden files under root.

    A file that vanishes mid-walk (a download being renamed) is skipped. A directory or file that
    exists but can't be read (permission denied, a cloud-sync glitch) is skipped too, and appended
    to `unreadable` so callers don't mistake it for deleted. Hidden directories aren't entered.
    """

    def on_error(err: OSError) -> None:
        if unreadable is not None and not isinstance(err, FileNotFoundError):
            unreadable.append(Path(err.filename))

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath, name)
            if not is_candidate(root, p):
                continue
            try:
                st = p.stat()
            except OSError as e:
                # Gone, a broken symlink, or a symlink loop: nothing there to index.
                if isinstance(e, FileNotFoundError) or e.errno in (errno.ELOOP, errno.ENOTDIR):
                    continue
                if unreadable is not None:
                    unreadable.append(p)
                continue
            if stat.S_ISREG(st.st_mode):
                cloud_only = bool(getattr(st, "st_flags", 0) & SF_DATALESS)
                yield FileInfo(p, st.st_size, st.st_mtime, cloud_only)
