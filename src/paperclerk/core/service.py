"""The one place business logic lives. CLI, FastAPI and MCP are thin shells over this class."""

from __future__ import annotations

import errno
import hashlib
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paperclerk.config import Settings
from paperclerk.db import connect, init_db, repo
from paperclerk.extract.cache import pages_marker
from paperclerk.ingest.parsers import Page, ParseError, Parser, parser_for
from paperclerk.ingest.scanner import (
    FileInfo,
    is_candidate,
    iter_candidate_files,
    no_cloud_downloads,
    sha256_file,
)
from paperclerk.llm.base import LLMClient

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


class ExtractionInProgress(RuntimeError):
    """Another run of classify_pending is reading documents."""


@dataclass
class ExtractRunResult:
    read: int = 0  # documents sent to the model
    accepted: int = 0  # entered the library
    review: int = 0  # answered, waiting for a person
    failed: int = 0  # no answer (tried again once on the next run)
    skipped: list[str] = field(default_factory=list)  # file gone or changed: rescan first
    reused: int = 0  # of `read`: answered from an earlier identical read, at no cost
    cost_usd: float = 0.0
    stopped: str | None = None  # why the run stopped early (the monthly budget)


class BudgetReached(Exception):
    pass


def _read_content(paths: list[str], sha256: str) -> bytes | None:
    """The bytes of the first path that still holds the content with this hash. Never
    downloads a cloud-only file (the read fails instead, as in a scan)."""
    for path in paths:
        try:
            p = Path(path)
            if p.stat().st_size > MAX_PARSE_BYTES:
                continue
            with no_cloud_downloads():
                data = p.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() == sha256:
            return data
    return None


class PaperClerk:
    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection | None = None,
        *,
        client_for: Callable[[str], LLMClient] | None = None,
    ) -> None:
        self.settings = settings
        self.conn = conn or connect(settings.db_path)
        init_db(self.conn)
        # The API serves requests from a thread pool on this one connection, and sqlite3
        # connections aren't safe to use from several threads at once: serialize every access.
        self._db_lock = threading.RLock()
        # One scan at a time. A scan takes the database lock per file, not for the whole scan,
        # so the API keeps answering (the document list fills in) while a long scan runs.
        self._scan_lock = threading.Lock()
        self._extract_lock = threading.Lock()
        if client_for is None:
            from paperclerk.llm.anthropic_client import client_for
        self._client_for = client_for

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
                # The text changed under existing answers: their checks no longer hold, so
                # they're read again (an answer the user verified stays).
                result.updated += 1
                repo.set_parse_result(self.conn, doc_id, **_fields(parsed), stale_reads=True)
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
    def classify_pending(self, limit: int | None = None) -> ExtractRunResult:
        """Read every document that has no current answer (repo.documents_to_extract), check
        each answer, and store it: in the library, or waiting for review with its reasons.

        A failed read is stored as a failure; the document is tried once more on the next run,
        then waits in the review queue. Model calls run without the database lock, so the API
        stays responsive. Documents already read aren't re-read when the prompt or the model
        changes: that's a separate, deliberate re-read.
        """
        if not self._extract_lock.acquire(blocking=False):
            raise ExtractionInProgress("documents are already being read")
        try:
            return self._classify_pending(limit)
        finally:
            self._extract_lock.release()

    def _classify_pending(self, limit: int | None) -> ExtractRunResult:
        from paperclerk.llm.pricing import PRICES, base_model

        result = ExtractRunResult()
        llm = self.settings.llm
        if llm.monthly_budget_usd is not None:
            unpriced = [
                m for m in (llm.model, llm.vision_model) if m and base_model(m) not in PRICES
            ]
            if unpriced:
                # Its calls would cost an unknown amount, so the budget couldn't be kept.
                result.stopped = f"no price known for {', '.join(unpriced)}: budget can't be kept"
                return result
        with self._db_lock:
            pending = repo.documents_to_extract(self.conn, limit)
        for doc_id in pending:
            try:
                self._classify_one(doc_id, result)
            except BudgetReached as e:
                result.stopped = str(e)
                break
            except sqlite3.Error:  # storing failed (locked, disk full): next run tries again
                log.exception("couldn't store the answer for document %s", doc_id)
                with self._db_lock:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                result.skipped.append(f"document {doc_id}: couldn't be stored")
        return result

    def estimate_pending(self, limit: int | None = None) -> tuple[int, float]:
        """(documents due, about what reading them costs in USD). Reads already made with the
        same content, prompt and models are reused, so they count as free."""
        from paperclerk.extract import cache
        from paperclerk.extract.estimate import document_cost
        from paperclerk.ingest.parsers.base import ParsedDocument

        llm = self.settings.llm
        total = 0.0
        with self._db_lock:
            pending = repo.documents_to_extract(self.conn, limit)
            for doc_id in pending:
                row = self.conn.execute(
                    "SELECT sha256, mime_type FROM documents WHERE id = ?", (doc_id,)
                ).fetchone()
                pages = repo.get_pages(self.conn, doc_id)
                path = self.conn.execute(
                    "SELECT MIN(path) FROM document_paths WHERE document_id = ?", (doc_id,)
                ).fetchone()[0]
                key = cache.cache_key(
                    row["sha256"], pages, file_name=Path(path).name if path else "",
                    model=llm.model, vision_model=llm.vision_model or None, effort=llm.effort,
                )  # fmt: skip
                if cache.get(self.conn, key) is None:
                    doc = ParsedDocument(row["mime_type"] or "", tuple(pages))
                    total += document_cost(
                        doc, model=llm.model, vision_model=llm.vision_model or None
                    )
        return len(pending), total

    def usage(self, since: str | None = None) -> list[dict[str, Any]]:
        """Model calls and cost per day and model, this month unless `since` (UTC) is given."""
        with self._db_lock:
            return repo.usage_summary(self.conn, since or repo.month_start())

    def _classify_one(self, doc_id: int, result: ExtractRunResult) -> None:
        from paperclerk.extract import cache
        from paperclerk.extract.classifier import extract
        from paperclerk.extract.estimate import document_cost
        from paperclerk.extract.review import review_reasons
        from paperclerk.ingest.parsers.base import ParsedDocument

        llm = self.settings.llm
        with self._db_lock:
            row = self.conn.execute(
                "SELECT sha256, mime_type, source FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            pages = repo.get_pages(self.conn, doc_id)
            paths = [
                r[0]
                for r in self.conn.execute(
                    "SELECT path FROM document_paths WHERE document_id = ? ORDER BY path",
                    (doc_id,),
                )
            ]
        marker = pages_marker(pages)
        doc = ParsedDocument(row["mime_type"] or "", tuple(pages))
        name = Path(paths[0]).name if paths else ""
        data = b""
        if doc.needs_vision:
            # Images are made from the file itself, so it must still hold this content.
            found = _read_content(paths, row["sha256"])
            if found is None and paths:
                result.skipped.append(f"{paths[0]}: changed or unavailable; scan first")
                return
            if found is None:  # not a local file (F3 sources): nothing to make images from
                with self._db_lock:
                    repo.insert_failure(
                        self.conn, doc_id, model=llm.model, prompt_version="",
                        error=f"no local file to read the scanned pages from ({row['source']})",
                        cost_usd=None,
                    )  # fmt: skip
                result.failed += 1
                return
            data = found

        key = cache.cache_key(
            row["sha256"], pages, file_name=name, model=llm.model,
            vision_model=llm.vision_model or None, effort=llm.effort,
        )  # fmt: skip
        with self._db_lock:
            answer = cache.get(self.conn, key)
            if answer is None and llm.monthly_budget_usd is not None:
                spent = repo.spent_since(self.conn, repo.month_start())
                next_cost = document_cost(doc, model=llm.model, vision_model=llm.vision_model)
                if spent + next_cost > llm.monthly_budget_usd:
                    raise BudgetReached(
                        f"monthly budget ${llm.monthly_budget_usd:.2f} reached "
                        f"(spent ${spent:.2f} this month)"
                    )
        error = None
        if answer is not None:
            result.reused += 1
        else:
            try:
                answer = extract(
                    doc,
                    data,
                    name,
                    model=llm.model,
                    vision_model=llm.vision_model or None,
                    effort=llm.effort,
                    client_for=self._client_for,
                )
            except Exception as e:  # one document's trouble never stops the run
                log.exception("extraction failed for document %s", doc_id)
                error = f"{type(e).__name__}: {e}"[:300]
        result.read += 1
        if answer is not None and answer.cost_usd is not None:
            result.cost_usd += answer.cost_usd
        if answer is not None and not answer.cached:
            # Paid for: log the calls (the budget counts them) and keep the read for reuse at
            # once, in a transaction of their own, so a failure to store the answer below never
            # loses them or makes the next run pay again. The key holds the pages read, so a
            # read of pages that have since changed is never reused for the new ones.
            with self._db_lock, self.conn:
                repo.insert_llm_calls(self.conn, doc_id, answer, commit=False)
                cache.put(self.conn, key, answer, commit=False)

        with self._db_lock:
            # The model read these pages without the lock held: if a scan changed them since,
            # the answer was checked against text the document no longer has. Store it as
            # history (stale), so the document stays due and is read again. The write lock is
            # taken first, so another process can't change the pages between check and store.
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            current = self.conn.execute(
                "SELECT parse_status FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            still_same = (
                current is not None
                and current["parse_status"] == repo.PARSE_OK
                and pages_marker(repo.get_pages(self.conn, doc_id)) == marker
            )
            if answer is None or answer.failed:
                if answer is not None:
                    error = answer.attempts[-1].error if answer.attempts else "not tried"
                repo.insert_failure(
                    self.conn, doc_id,
                    model=answer.requested_model if answer else llm.model,
                    prompt_version=answer.prompt_version if answer else "",
                    error=error or "unknown",
                    cost_usd=answer.cost_usd if answer else None,
                    stale=not still_same,
                )  # fmt: skip
                result.failed += 1
                return
            reasons = review_reasons(answer, llm.review_threshold)
            ex = answer.extraction
            repo.insert_extraction(
                self.conn, doc_id, ex.model_dump(mode="json"),
                model=answer.model, prompt_version=answer.prompt_version,
                review_reasons=reasons,
                evidence_json=ex.evidence.model_dump_json() if ex.evidence else None,
                raw_json=ex.model_dump_json(),
                cost_usd=answer.cost_usd,
                stale=not still_same,
            )  # fmt: skip
        if not still_same:
            result.skipped.append(f"document {doc_id}: changed while it was read; read again")
        elif reasons:
            result.review += 1
        else:
            result.accepted += 1

    def review_queue(self) -> list[dict[str, Any]]:
        with self._db_lock:
            return repo.review_queue(self.conn)

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
