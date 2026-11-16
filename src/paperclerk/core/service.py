"""The one place business logic lives. CLI, FastAPI and MCP are thin shells over this class."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paperclerk.config import Settings
from paperclerk.db import connect, init_db, repo
from paperclerk.ingest.parsers import parser_for
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


# Parse outcome that is about the file, not its content (locked, permission denied, vanished
# mid-scan). Never stored: nothing is recorded for the file, so the next scan simply retries.
_TRANSIENT = "transient"


@dataclass(frozen=True)
class _Parsed:
    text: str | None
    mime_type: str | None
    status: str


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
          parser yet: that is re-parsed (without re-hashing) so it gets text once F1 lands.
        - New or changed files are hashed. Known content is never parsed twice; a path whose
          content changed points at the new document, and the old document keeps its text.
        - Content that failed to parse is retried only when the file changes.
        - One unreadable or malformed file never stops the scan.
        """
        with self._db_lock:
            result = ScanResult()
            for root in self.settings.watched_folders:
                if not root.is_dir():
                    # Don't forget its paths: the folder may be on an unplugged drive.
                    log.warning("watched folder missing: %s", root)
                    continue
                present: set[str] = set()
                for info in iter_candidate_files(root):
                    present.add(str(info.path))
                    try:
                        self._scan_file(info, result)
                    except sqlite3.Error:
                        # e.g. "database is locked" while another process writes: skip this
                        # file (retried next scan) rather than abandon the rest of the scan.
                        self.conn.rollback()
                        log.warning("database error on %s", info.path, exc_info=True)
                        result.failed.append(str(info.path))
                # Only forget paths this walk would have yielded, so a watched folder nested in a
                # hidden folder of another watched folder doesn't lose its paths to that walk.
                gone = [
                    p
                    for p in repo.paths_under(self.conn, root)
                    if p not in present and is_candidate(root, Path(p))
                ]
                result.removed += repo.delete_paths(self.conn, gone)
            return result

    def _scan_file(self, info: FileInfo, result: ScanResult) -> None:
        path = str(info.path)
        known = repo.get_path(self.conn, path)
        unchanged = (
            known is not None
            and known["mtime"] == info.mtime
            and known["size_bytes"] == info.size_bytes
        )
        if unchanged:
            if known["parse_status"] in (repo.PARSE_OK, repo.PARSE_FAILED):
                return
            # Never parsed (pre-migration row) or no parser last time: try again, no re-hash.
            parsed = self._parse(info)
            if parsed.status == _TRANSIENT:
                result.failed.append(path)
                return
            if parsed.status == repo.PARSE_UNSUPPORTED:
                result.skipped.append(path)
                if known["parse_status"] is None:
                    repo.set_parse_result(self.conn, known["document_id"], **_fields(parsed))
                return
            result.seen += 1
            result.updated += 1
            self._record_outcome(path, parsed, result)
            repo.set_parse_result(self.conn, known["document_id"], **_fields(parsed))
            return

        result.seen += 1
        try:
            sha256 = sha256_file(info.path)
        except OSError:
            log.warning("can't read %s", info.path, exc_info=True)
            result.failed.append(path)
            return
        existing = repo.get_document_by_sha(self.conn, sha256)
        if existing is not None and existing["parse_status"] in (repo.PARSE_OK, repo.PARSE_FAILED):
            # Same content as a document we already parsed (a copy, a move, a revert).
            doc_id, is_new = existing["id"], False
        else:
            parsed = self._parse(info)
            self._record_outcome(path, parsed, result)
            if parsed.status == _TRANSIENT:
                return
            doc_id, is_new = repo.upsert_document(
                self.conn,
                sha256=sha256,
                source="local",
                size_bytes=info.size_bytes,
                **_fields(parsed),
            )
        repo.set_path(self.conn, path, doc_id, mtime=info.mtime, size_bytes=info.size_bytes)
        if is_new:
            result.new += 1
        else:
            result.updated += 1

    @staticmethod
    def _parse(info: FileInfo) -> _Parsed:
        parser = parser_for(info.path)
        if parser is None:
            return _Parsed(None, None, repo.PARSE_UNSUPPORTED)
        try:
            parsed = parser.parse(info.path)
        except NotImplementedError:
            return _Parsed(None, None, repo.PARSE_UNSUPPORTED)
        except OSError:
            # A problem reading the file, not with its content.
            log.warning("can't read %s", info.path, exc_info=True)
            return _Parsed(None, None, _TRANSIENT)
        except Exception:
            # Corrupt, encrypted or otherwise malformed: record it and keep going.
            log.warning("failed to parse %s", info.path, exc_info=True)
            return _Parsed(None, None, repo.PARSE_FAILED)
        return _Parsed(parsed.text, parsed.mime_type, repo.PARSE_OK)

    @staticmethod
    def _record_outcome(path: str, parsed: _Parsed, result: ScanResult) -> None:
        if parsed.status == repo.PARSE_UNSUPPORTED:
            result.skipped.append(path)
        elif parsed.status in (repo.PARSE_FAILED, _TRANSIENT):
            result.failed.append(path)

    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db_lock:
            return repo.list_documents(self.conn, limit=limit)

    # ---- F2: classify / extract ---------------------------------------------------------------
    def classify_pending(self) -> int:
        raise NotImplementedError("week 1 (F2)")

    # ---- F4: organize -------------------------------------------------------------------------
    def propose_plan(self) -> Any:
        raise NotImplementedError("week 2 (F4)")

    def undo(self, batch_id: str) -> int:
        raise NotImplementedError("week 2 (F4)")


def _fields(parsed: _Parsed) -> dict[str, Any]:
    return {"text": parsed.text, "mime_type": parsed.mime_type, "parse_status": parsed.status}
