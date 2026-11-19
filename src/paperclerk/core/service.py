"""The one place business logic lives. CLI, FastAPI and MCP are thin shells over this class."""

from __future__ import annotations

import errno
import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paperclerk.config import Settings
from paperclerk.db import connect, init_db, repo
from paperclerk.ingest.parsers import Page, ParseError, Parser, parser_for
from paperclerk.ingest.scanner import (
    FileInfo,
    is_candidate,
    iter_candidate_files,
    no_cloud_downloads,
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
    reasons: dict[str, str] = field(default_factory=dict)  # failed path -> why, for the user
    # Only in the cloud (not downloaded): left alone so the scan doesn't download them. Known
    # ones keep their documents; new ones are indexed once downloaded.
    cloud_only: list[str] = field(default_factory=list)

    def fail(self, path: str, reason: str) -> None:
        self.failed.append(path)
        self.reasons.setdefault(path, reason)


def _read_error(e: OSError) -> str:
    if isinstance(e, _ChangedWhileReading):
        return "changed while being read; retried next scan"
    if isinstance(e, PermissionError):
        return "permission denied"
    return f"can't read ({e.strerror.lower()})" if e.strerror else "can't read"


# Files larger than this are hashed (so duplicates are still found) but not parsed: they
# would be read into memory whole, and bills, invoices and receipts are never this big.
MAX_PARSE_BYTES = 100 * 1024 * 1024
_TOO_LARGE_PREFIX = "too large to read"
TOO_LARGE = f"{_TOO_LARGE_PREFIX} (over {MAX_PARSE_BYTES // (1024 * 1024)} MB)"


def _outdated(known: sqlite3.Row, parser: Parser | None, info: FileInfo) -> bool:
    """Whether an unchanged, already parsed file should be parsed again."""
    if parser is None:
        return False
    error = known["parse_error"] or ""
    if error.startswith(_TOO_LARGE_PREFIX) and info.size_bytes <= MAX_PARSE_BYTES:
        return True  # the size limit was raised since
    return (known["parser_version"] or 0) < parser.version


class ScanInProgress(RuntimeError):
    """Another scan of this PaperClerk is running; its result will include this one's work."""


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
    version: int | None = None  # Parser.version that produced this outcome


class PaperClerk:
    def __init__(self, settings: Settings, conn: sqlite3.Connection | None = None) -> None:
        self.settings = settings
        self.conn = conn or connect(settings.db_path)
        init_db(self.conn)
        # The API serves requests from a thread pool on this one connection, and sqlite3
        # connections aren't safe to use from several threads at once: serialize every access.
        self._db_lock = threading.RLock()
        # One scan at a time. A scan takes the database lock per file, not for the whole scan,
        # so the API keeps answering (the document list fills in) while a long scan runs.
        self._scan_lock = threading.Lock()

    # ---- F1: ingest -------------------------------------------------------------------------
    def scan(self) -> ScanResult:
        """Bring the index in line with the watched folders.

        - Unchanged files (same mtime and size) cost nothing, except when their parser is newer
          than the one that parsed them (Parser.version), they had no parser, or they were too
          large under a smaller limit. Those are re-parsed (and re-hashed before the result is
          stored, since an unchanged mtime and size don't prove unchanged bytes); a document
          whose pages come out the same only has its version updated.
        - Files only in the cloud (iCloud, Dropbox, ...) aren't read, so a scan never downloads
          them; they're reported in `cloud_only` and keep their paths.
        - New or changed files are hashed. Known content is never parsed twice; a path whose
          content changed points at the new document, and the old document keeps its pages.
        - Content that failed to parse is retried only when the file changes.
        - One unreadable or malformed file never stops the scan. Files and folders that exist
          but can't be read are reported in `failed` and keep their paths (not treated as deleted).
        """
        if not self._scan_lock.acquire(blocking=False):
            raise ScanInProgress("a scan is already running")
        try:
            return self._scan()
        finally:
            self._scan_lock.release()

    def _scan(self) -> ScanResult:
        with no_cloud_downloads():
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
                    with self._db_lock:
                        try:
                            self._scan_file(info, result)
                        except sqlite3.Error:
                            # e.g. "database is locked" while another process writes: skip
                            # this file (retried next scan) rather than abandon the scan.
                            self.conn.rollback()
                            log.warning("database error on %s", info.path, exc_info=True)
                            path = str(info.path)
                            result.failed.append(path)
                            # Replaces a parse reason recorded before the write failed.
                            result.reasons[path] = "database busy; retried next scan"
                for u in unreadable:
                    log.warning("can't read %s", u)
                    result.fail(str(u), "can't read (permission denied or unavailable)")
                # Only forget paths this walk would have yielded, so a watched folder nested in a
                # hidden folder of another watched folder doesn't lose its paths to that walk, and
                # never paths the walk couldn't see into: unreadable isn't deleted.
                with self._db_lock:
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
            result.cloud_only = list(dict.fromkeys(result.cloud_only))
            return result

    def _scan_file(self, info: FileInfo, result: ScanResult) -> None:
        path = str(info.path)
        if info.cloud_only:
            result.cloud_only.append(path)
            return
        known = repo.get_path(self.conn, path)
        unchanged = (
            known is not None
            and known["mtime"] == info.mtime
            and known["size_bytes"] == info.size_bytes
        )
        parser = parser_for(info.path)
        if unchanged and known["parse_status"] in (repo.PARSE_OK, repo.PARSE_FAILED):
            if not _outdated(known, parser, info):
                return
        elif unchanged and parser is None:
            result.skipped.append(path)
            return
        if not unchanged:
            result.seen += 1

        # Read the file once: the hash and the parse below use these same bytes, so a file
        # rewritten mid-scan can't pair one content's hash with another content's text.
        try:
            content = _read(info)
        except OSError as e:
            if e.errno == errno.EDEADLK:
                # Evicted to the cloud after the walk saw it: the read was refused rather than
                # downloading it (no_cloud_downloads).
                result.cloud_only.append(path)
                return
            log.warning("can't read %s", info.path, exc_info=True)
            result.fail(path, _read_error(e))
            return

        parsed: _Parsed | None = None
        if unchanged:
            # No parser last time, a newer parser now, or a raised size limit: try again.
            parsed = self._parse(info, content.data)
            if parsed.status == repo.PARSE_UNSUPPORTED:
                result.skipped.append(path)
                if known["parse_status"] is None:
                    repo.set_parse_result(self.conn, known["document_id"], **_fields(parsed))
                elif parser is not None:
                    # A newer version that isn't implemented yet: keep the outcome the old
                    # one gave, and don't re-read the file on every scan.
                    repo.set_parser_version(self.conn, known["document_id"], parser.version)
                return
            # A real outcome is about to be attached to a document that copies may share. Same
            # mtime and size doesn't prove same bytes (`cp -p`, a guessed migration mapping), so
            # it only lands on the known document if the hash matches.

        if unchanged:
            result.seen += 1
        if parsed is not None and content.sha256 == known["sha256"]:
            self._record_outcome(path, parsed, result)
            doc_id = known["document_id"]
            if self._same_outcome(known, parsed):
                repo.set_parser_version(self.conn, doc_id, parsed.version)
            else:
                # TODO(F2): the text changed under existing extractions: mark them stale (never
                # overwrite human_verified fields) so they're re-extracted or reviewed.
                result.updated += 1
                repo.set_parse_result(self.conn, doc_id, **_fields(parsed))
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

    def _same_outcome(self, known: sqlite3.Row, parsed: _Parsed) -> bool:
        if (parsed.status, parsed.error) != (known["parse_status"], known["parse_error"]):
            return False
        if parsed.mime_type is not None and parsed.mime_type != known["mime_type"]:
            return False
        return repo.get_pages(self.conn, known["document_id"]) == list(_fields(parsed)["pages"])

    @staticmethod
    def _parse(info: FileInfo, data: bytes | None) -> _Parsed:
        parser = parser_for(info.path)
        if parser is None:
            return _Parsed(None, None, repo.PARSE_UNSUPPORTED)
        v = parser.version
        if data is None:
            return _Parsed(None, None, repo.PARSE_FAILED, TOO_LARGE, v)
        try:
            parsed = parser.parse(data, info.path)
        except NotImplementedError:
            return _Parsed(None, None, repo.PARSE_UNSUPPORTED)
        except ParseError as e:
            # A known problem with the content (e.g. password protected).
            log.warning("can't parse %s: %s", info.path, e)
            return _Parsed(None, None, repo.PARSE_FAILED, str(e), v)
        except Exception:
            # Corrupt or otherwise malformed: record it and keep going. Parsers get bytes, not
            # the file, so even an OSError here is about the content.
            log.warning("failed to parse %s", info.path, exc_info=True)
            return _Parsed(None, None, repo.PARSE_FAILED, "damaged or unreadable file", v)
        return _Parsed(parsed.pages, parsed.mime_type, repo.PARSE_OK, None, v)

    @staticmethod
    def _record_outcome(path: str, parsed: _Parsed, result: ScanResult) -> None:
        if parsed.status == repo.PARSE_UNSUPPORTED:
            result.skipped.append(path)
        elif parsed.status == repo.PARSE_FAILED:
            result.fail(path, parsed.error or "couldn't parse")

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
        "parser_version": parsed.version,
    }
