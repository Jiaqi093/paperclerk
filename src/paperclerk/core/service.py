"""The one place business logic lives. CLI, FastAPI and MCP are thin shells over this class."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paperclerk.config import Settings
from paperclerk.db import connect, init_db, repo
from paperclerk.ingest.parsers import Page, ParseError, parser_for
from paperclerk.ingest.scanner import (
    FileInfo,
    is_candidate,
    iter_candidate_files,
    sha256_file,
)

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    seen: int = 0  # files that needed work this scan (new, changed, or re-parsed)
    new: int = 0  # new documents (content never indexed before)
    updated: int = 0  # known content at a new path, or re-parsed with a new outcome
    removed: int = 0  # paths no longer on disk (their documents are kept)
    skipped: list[str] = field(default_factory=list)  # no parser implementation yet
    failed: list[str] = field(default_factory=list)  # couldn't read or parse (logged)


# Files larger than this are hashed (so duplicates are still found) but not parsed: they
# would be read into memory whole, and bills, invoices and receipts are never this big.
MAX_PARSE_BYTES = 100 * 1024 * 1024


class _ChangedWhileReading(OSError):
    """The file was written to while being read: what was read may be half old, half new."""


@dataclass(frozen=True)
class _Content:
    sha256: str
    data: bytes | None  # None: over MAX_PARSE_BYTES, hashed in chunks instead
    mtime: float  # the stat these bytes belong to
    size_bytes: int


def _read(info: FileInfo) -> _Content:
    """Read a file once for hashing and parsing.

    If it changed while being read (a save in progress, or a cloud-sync folder updating the
    file as it downloads), it is read once more from a fresh stat. Raises OSError if it can't
    be read or keeps changing: nothing is recorded, and the next scan retries.
    """
    try:
        return _read_once(info.path, info.mtime, info.size_bytes)
    except _ChangedWhileReading:
        st = info.path.stat()
        return _read_once(info.path, st.st_mtime, st.st_size)


def _read_once(path: Path, mtime: float, size: int) -> _Content:
    if size > MAX_PARSE_BYTES:
        sha256, data = sha256_file(path), None
    else:
        with path.open("rb") as f:
            data = f.read(size + 1)  # never more than expected, even if the file grew
        sha256 = hashlib.sha256(data).hexdigest()
    st = path.stat()
    if (st.st_mtime, st.st_size) != (mtime, size) or (data is not None and len(data) != size):
        raise _ChangedWhileReading(f"{path} changed while it was being read")
    return _Content(sha256, data, mtime, size)


@dataclass(frozen=True)
class _Parsed:
    pages: tuple[Page, ...] | None
    mime_type: str | None
    status: str
    error: str | None = None  # why parsing failed, for the user (status 'failed' only)


class PaperClerk:
    def __init__(self, settings: Settings, conn: sqlite3.Connection | None = None) -> None:
        self.settings = settings
        self.conn = conn or connect(settings.db_path)
        init_db(self.conn)
        # The API serves requests from a thread pool on this one connection, and sqlite3
        # connections aren't safe to use from several threads at once: serialize every access.
        self._db_lock = threading.RLock()

    # ---- F1: ingest -------------------------------------------------------------------------
    def scan(self) -> ScanResult:
        """Bring the index in line with the watched folders.

        - Unchanged files (same mtime and size) cost nothing, except content whose type had no
          parser yet: that is re-parsed so it gets pages once F1 lands (and re-hashed before
          the result is stored, since an unchanged mtime and size don't prove unchanged bytes).
        - New or changed files are hashed. Known content is never parsed twice; a path whose
          content changed points at the new document, and the old document keeps its pages.
        - Content that failed to parse is retried only when the file changes.
        - One unreadable or malformed file never stops the scan. Files and folders that exist
          but can't be read are reported in `failed` and keep their paths (not treated as deleted).
        """
        with self._db_lock:
            result = ScanResult()
            for root in self.settings.watched_folders:
                if not root.is_dir():
                    # Don't forget its paths: the folder may be on an unplugged drive.
                    log.warning("watched folder missing: %s", root)
                    continue
                present: set[str] = set()
                unreadable: list[Path] = []
                for info in iter_candidate_files(root, unreadable):
                    present.add(str(info.path))
                    try:
                        self._scan_file(info, result)
                    except sqlite3.Error:
                        # e.g. "database is locked" while another process writes: skip this
                        # file (retried next scan) rather than abandon the rest of the scan.
                        self.conn.rollback()
                        log.warning("database error on %s", info.path, exc_info=True)
                        result.failed.append(str(info.path))
                for u in unreadable:
                    log.warning("can't read %s", u)
                    result.failed.append(str(u))
                # Only forget paths this walk would have yielded, so a watched folder nested in a
                # hidden folder of another watched folder doesn't lose its paths to that walk, and
                # never paths the walk couldn't see into: unreadable isn't deleted.
                gone = [
                    p
                    for p in repo.paths_under(self.conn, root)
                    if p not in present
                    and is_candidate(root, Path(p))
                    and not any(Path(p).is_relative_to(u) for u in unreadable)
                ]
                result.removed += repo.delete_paths(self.conn, gone)
            # Overlapping watched folders can report the same path twice.
            result.skipped = list(dict.fromkeys(result.skipped))
            result.failed = list(dict.fromkeys(result.failed))
            return result

    def _scan_file(self, info: FileInfo, result: ScanResult) -> None:
        path = str(info.path)
        known = repo.get_path(self.conn, path)
        unchanged = (
            known is not None
            and known["mtime"] == info.mtime
            and known["size_bytes"] == info.size_bytes
        )
        if unchanged and known["parse_status"] in (repo.PARSE_OK, repo.PARSE_FAILED):
            return
        if unchanged and parser_for(info.path) is None:
            result.skipped.append(path)
            return
        if not unchanged:
            result.seen += 1

        # Read the file once: the hash and the parse below use these same bytes, so a file
        # rewritten mid-scan can't pair one content's hash with another content's text.
        try:
            content = _read(info)
        except OSError:
            log.warning("can't read %s", info.path, exc_info=True)
            result.failed.append(path)
            return

        parsed: _Parsed | None = None
        if unchanged:
            # Never parsed (pre-migration row) or no parser last time: try again.
            parsed = self._parse(info, content.data)
            if parsed.status == repo.PARSE_UNSUPPORTED:
                result.skipped.append(path)
                if known["parse_status"] is None:
                    repo.set_parse_result(self.conn, known["document_id"], **_fields(parsed))
                return
            # A real outcome is about to be attached to a document that copies may share. Same
            # mtime and size doesn't prove same bytes (`cp -p`, a guessed migration mapping), so
            # it only lands on the known document if the hash matches.

        if unchanged:
            result.seen += 1
        if parsed is not None and content.sha256 == known["sha256"]:
            result.updated += 1
            self._record_outcome(path, parsed, result)
            repo.set_parse_result(self.conn, known["document_id"], **_fields(parsed))
            return
        existing = repo.get_document_by_sha(self.conn, content.sha256)
        if existing is not None and existing["parse_status"] in (repo.PARSE_OK, repo.PARSE_FAILED):
            # Same content as a document we already parsed (a copy, a move, a revert).
            doc_id, is_new = existing["id"], False
        else:
            if parsed is None:
                parsed = self._parse(info, content.data)
            self._record_outcome(path, parsed, result)
            doc_id, is_new = repo.upsert_document(
                self.conn,
                sha256=content.sha256,
                source="local",
                size_bytes=content.size_bytes,
                **_fields(parsed),
            )
        repo.set_path(self.conn, path, doc_id, mtime=content.mtime, size_bytes=content.size_bytes)
        if is_new:
            result.new += 1
        else:
            result.updated += 1

    @staticmethod
    def _parse(info: FileInfo, data: bytes | None) -> _Parsed:
        parser = parser_for(info.path)
        if parser is None:
            return _Parsed(None, None, repo.PARSE_UNSUPPORTED)
        if data is None:
            limit = MAX_PARSE_BYTES // (1024 * 1024)
            return _Parsed(None, None, repo.PARSE_FAILED, f"too large to read (over {limit} MB)")
        try:
            parsed = parser.parse(data, info.path)
        except NotImplementedError:
            return _Parsed(None, None, repo.PARSE_UNSUPPORTED)
        except ParseError as e:
            # A known problem with the content (e.g. password protected).
            log.warning("can't parse %s: %s", info.path, e)
            return _Parsed(None, None, repo.PARSE_FAILED, str(e))
        except Exception:
            # Corrupt or otherwise malformed: record it and keep going. Parsers get bytes, not
            # the file, so even an OSError here is about the content.
            log.warning("failed to parse %s", info.path, exc_info=True)
            return _Parsed(None, None, repo.PARSE_FAILED, "damaged or unreadable file")
        return _Parsed(parsed.pages, parsed.mime_type, repo.PARSE_OK)

    @staticmethod
    def _record_outcome(path: str, parsed: _Parsed, result: ScanResult) -> None:
        if parsed.status == repo.PARSE_UNSUPPORTED:
            result.skipped.append(path)
        elif parsed.status == repo.PARSE_FAILED:
            result.failed.append(path)

    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db_lock:
            return repo.list_documents(self.conn, limit=limit)

    # ---- F2: classify / extract ---------------------------------------------------------------
    def classify_pending(self) -> int:
        raise NotImplementedError("F2: not implemented yet")

    # ---- F4: organize -------------------------------------------------------------------------
    def propose_plan(self) -> Any:
        raise NotImplementedError("F4: not implemented yet")

    def undo(self, batch_id: str) -> int:
        raise NotImplementedError("F4: not implemented yet")


def _fields(parsed: _Parsed) -> dict[str, Any]:
    # A document's pages always match its parse outcome: no outcome but ok keeps old pages.
    pages = parsed.pages if parsed.status == repo.PARSE_OK else ()
    return {
        "pages": pages,
        "mime_type": parsed.mime_type,
        "parse_status": parsed.status,
        "parse_error": parsed.error,
    }
