"""Small data-access helpers. Keep SQL here, not in business logic.

Model: a *document* is one unique content (sha256) and never changes identity; its pages
(text per page, in document_pages) and parse outcome belong to that content. *document_paths*
records where each content currently sits on disk: one document can have many paths (copies),
a path holds exactly one document, and when a file's content changes its path simply points
at a different document.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from paperclerk.models import PAGE_SEPARATOR, Page

# documents.parse_status values. NULL (rows from before migration 1) is treated as not parsed.
PARSE_OK = "ok"
PARSE_UNSUPPORTED = "unsupported"  # no parser implementation for this file type yet
PARSE_FAILED = "failed"  # the parser raised on this content


def get_document_by_sha(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, parse_status FROM documents WHERE sha256 = ?", (sha256,)
    ).fetchone()


def upsert_document(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    source: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    pages: Sequence[Page] | None = None,
    source_ref: str | None = None,
    parse_status: str | None = None,
    parse_error: str | None = None,
    parser_version: int | None = None,
) -> tuple[int, bool]:
    """Insert a document for this content, or fill in fields on the existing one.

    Never changes an existing document's sha256, source or source_ref; None arguments leave
    stored values alone (`pages=None` keeps the stored pages, a list replaces them;
    `parse_error` and `parser_version` are replaced whenever `parse_status` is given, since
    they describe it). The row
    and its pages are written in one transaction. Returns (document_id, is_new).
    """
    with conn:
        row = get_document_by_sha(conn, sha256)
        if row is not None:
            doc_id, is_new = row["id"], False
            conn.execute(
                "UPDATE documents SET size_bytes = COALESCE(?, size_bytes), "
                "mime_type = COALESCE(?, mime_type), "
                "parse_error = CASE WHEN ? IS NULL THEN parse_error ELSE ? END, "
                "parser_version = CASE WHEN ? IS NULL THEN parser_version ELSE ? END, "
                "parse_status = COALESCE(?, parse_status), updated_at = datetime('now') "
                "WHERE id = ?",
                (
                    size_bytes,
                    mime_type,
                    parse_status,
                    parse_error,
                    parse_status,
                    parser_version,
                    parse_status,
                    doc_id,
                ),
            )
        else:
            cur = conn.execute(
                "INSERT INTO documents "
                "(sha256, source, source_ref, mime_type, size_bytes, parse_status, parse_error, "
                "parser_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sha256,
                    source,
                    source_ref,
                    mime_type,
                    size_bytes,
                    parse_status,
                    parse_error,
                    parser_version,
                ),
            )
            doc_id, is_new = int(cur.lastrowid), True
        if pages is not None:
            _replace_pages(conn, doc_id, pages)
    return doc_id, is_new


def set_parse_result(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    pages: Sequence[Page] | None,
    mime_type: str | None,
    parse_status: str,
    parse_error: str | None = None,
    parser_version: int | None = None,
    stale_reads: bool = False,
) -> None:
    """Record a new parse outcome; the document's pages become exactly `pages` (None: none).
    `stale_reads`: the text changed, so the document's reads no longer hold (in the same
    transaction, so no reader sees new pages next to answers checked against the old ones)."""
    with conn:
        conn.execute(
            "UPDATE documents SET mime_type = COALESCE(?, mime_type), parse_status = ?, "
            "parse_error = ?, parser_version = ?, updated_at = datetime('now') WHERE id = ?",
            (mime_type, parse_status, parse_error, parser_version, document_id),
        )
        _replace_pages(conn, document_id, pages or [])
        if stale_reads:
            _mark_stale(conn, document_id)


def set_parser_version(conn: sqlite3.Connection, document_id: int, version: int) -> None:
    """Record that the current parser version gives this document the outcome it has."""
    with conn:
        conn.execute("UPDATE documents SET parser_version = ? WHERE id = ?", (version, document_id))


def _replace_pages(conn: sqlite3.Connection, document_id: int, pages: Sequence[Page]) -> None:
    # No commit: callers wrap this and the documents row in one transaction.
    conn.execute("DELETE FROM document_pages WHERE document_id = ?", (document_id,))
    conn.executemany(
        "INSERT INTO document_pages (document_id, page_no, text, needs_vision) VALUES (?, ?, ?, ?)",
        [(document_id, i, p.text, int(p.needs_vision)) for i, p in enumerate(pages, start=1)],
    )


def get_pages(conn: sqlite3.Connection, document_id: int) -> list[Page]:
    """A document's pages in order (pages[0] is page 1); empty if it has no text."""
    rows = conn.execute(
        "SELECT text, needs_vision FROM document_pages WHERE document_id = ? ORDER BY page_no",
        (document_id,),
    )
    return [Page(r["text"], bool(r["needs_vision"])) for r in rows]


def document_text(conn: sqlite3.Connection, document_id: int) -> str | None:
    """A document's full text (its pages joined), or None if it has no pages."""
    pages = get_pages(conn, document_id)
    return PAGE_SEPARATOR.join(p.text for p in pages) if pages else None


def get_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    """What the database knows about the file at `path`: its document, mtime, size, state."""
    return conn.execute(
        "SELECT p.document_id, p.mtime, p.size_bytes, d.sha256, d.parse_status, "
        "d.parse_error, d.parser_version, d.mime_type "
        "FROM document_paths p JOIN documents d ON d.id = p.document_id WHERE p.path = ?",
        (path,),
    ).fetchone()


def set_path(
    conn: sqlite3.Connection, path: str, document_id: int, *, mtime: float, size_bytes: int
) -> None:
    """Record that `path` currently holds `document_id` (replacing whatever it held before)."""
    conn.execute(
        "INSERT INTO document_paths (path, document_id, mtime, size_bytes) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET document_id = excluded.document_id, "
        "mtime = excluded.mtime, size_bytes = excluded.size_bytes, seen_at = datetime('now')",
        (path, document_id, mtime, size_bytes),
    )
    conn.commit()


def paths_under(conn: sqlite3.Connection, root: Path) -> list[str]:
    """Paths inside `root` (by path component: /a holds /a/b, not /ab/c).

    A range on the primary key: every string starting with "<root>/" sorts between it and
    "<root>0" ("0" is the character after "/"), so this doesn't read the whole table.
    """
    prefix = str(root).rstrip("/") + "/"
    rows = conn.execute(
        "SELECT path FROM document_paths WHERE path >= ? AND path < ?",
        (prefix, prefix[:-1] + "0"),
    )
    return [r["path"] for r in rows if Path(r["path"]).is_relative_to(root)]


def delete_paths(conn: sqlite3.Connection, paths: list[str]) -> int:
    """Forget these paths (files no longer on disk). Their documents are kept as history."""
    conn.executemany("DELETE FROM document_paths WHERE path = ?", [(p,) for p in paths])
    conn.commit()
    return len(paths)


def list_documents(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    """Current documents, newest first: local content still on disk, plus non-local sources.

    Past versions and deleted files stay in the database (history, extractions) but aren't
    listed. `path` is one current location of the content.
    """
    rows = conn.execute(
        "SELECT d.id, (SELECT MIN(p.path) FROM document_paths p WHERE p.document_id = d.id) "
        "AS path, d.source, d.mime_type, d.size_bytes, d.parse_status, d.parse_error, "
        "EXISTS (SELECT 1 FROM document_pages g WHERE g.document_id = d.id AND g.needs_vision) "
        "AS needs_vision, d.created_at FROM documents d "
        "WHERE d.source != 'local' "
        "OR EXISTS (SELECT 1 FROM document_paths p WHERE p.document_id = d.id) "
        "ORDER BY d.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- F2: extractions ----------------------------------------------------------------------

# A document whose reads failed this many times in a row is left for review, not tried again
# on every run (the first retry catches network trouble and busy servers).
MAX_FAILED_READS = 2

_EXTRACTION_FIELDS = (
    "doc_type", "vendor", "amount", "currency", "issue_date", "due_date", "is_recurring",
    "period", "confidence",
)  # fmt: skip


def _now() -> str:
    # Microseconds, so a failure and an extraction in the same second still order correctly.
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def insert_extraction(
    conn: sqlite3.Connection,
    document_id: int,
    values: dict[str, Any],
    *,
    model: str | None,
    prompt_version: str,
    review_reasons: Sequence[str],
    evidence_json: str | None,
    raw_json: str | None,
    cost_usd: float | None,
    stale: bool = False,
) -> int:
    """Store one read of a document. Earlier reads are kept as history. `stale`: the read
    no longer matches the document's text (kept for its cost and as history only)."""
    import json

    cols = list(_EXTRACTION_FIELDS)
    params = [values.get(c) for c in cols]
    cur = conn.execute(
        f"INSERT INTO extractions (document_id, {', '.join(cols)}, needs_review, model, "
        "prompt_version, review_reasons, evidence_json, raw_json, cost_usd, stale, created_at) "
        f"VALUES (?, {', '.join('?' * len(cols))}, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document_id, *params, int(bool(review_reasons)), model, prompt_version,
            json.dumps(list(review_reasons)), evidence_json, raw_json, cost_usd, int(stale),
            _now(),
        ),
    )  # fmt: skip
    conn.commit()
    return int(cur.lastrowid)


def insert_failure(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    model: str,
    prompt_version: str,
    error: str,
    cost_usd: float | None,
    stale: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO extraction_failures (document_id, model, prompt_version, error, cost_usd, "
        "stale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (document_id, model, prompt_version, error, cost_usd, int(stale), _now()),
    )
    conn.commit()


def mark_extractions_stale(conn: sqlite3.Connection, document_id: int) -> int:
    """The document's text changed: its reads were checked against text it no longer has, and
    its failed reads were of other text, so they no longer count against it. A read the user
    verified stays: their answer outranks any re-read. Returns the answers made stale."""
    with conn:
        return _mark_stale(conn, document_id)


def _mark_stale(conn: sqlite3.Connection, document_id: int) -> int:
    conn.execute(
        "UPDATE extraction_failures SET stale = 1 WHERE document_id = ? AND stale = 0",
        (document_id,),
    )
    cur = conn.execute(
        "UPDATE extractions SET stale = 1 "
        "WHERE document_id = ? AND human_verified = 0 AND stale = 0",
        (document_id,),
    )
    return cur.rowcount


# The answer that counts for a document: the newest the user verified, else the newest read
# whose checks still hold.
_CURRENT = (
    "SELECT * FROM extractions WHERE document_id = ? AND (human_verified = 1 OR stale = 0) "
    "ORDER BY human_verified DESC, created_at DESC, id DESC LIMIT 1"
)


def current_extraction(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return conn.execute(_CURRENT, (document_id,)).fetchone()


def failed_reads_since_last_answer(conn: sqlite3.Connection, document_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM extraction_failures f WHERE f.document_id = ? AND f.stale = 0 "
        "AND f.created_at > "
        "COALESCE((SELECT MAX(created_at) FROM extractions e WHERE e.document_id = ?), '')",
        (document_id, document_id),
    ).fetchone()[0]


def documents_to_extract(conn: sqlite3.Connection, limit: int | None = None) -> list[int]:
    """Parsed documents with no current answer, oldest first: never read, read before their
    text changed, or failed fewer than MAX_FAILED_READS times since. A new prompt or model
    doesn't make a document due again: re-reading what's already read is a separate choice."""
    rows = conn.execute(
        "SELECT d.id FROM documents d WHERE d.parse_status = ? "
        "AND (d.source != 'local' "
        "     OR EXISTS (SELECT 1 FROM document_paths p WHERE p.document_id = d.id)) "
        "AND NOT EXISTS (SELECT 1 FROM extractions e WHERE e.document_id = d.id "
        "                AND (e.human_verified = 1 OR e.stale = 0)) "
        "AND (SELECT COUNT(*) FROM extraction_failures f WHERE f.document_id = d.id "
        "     AND f.stale = 0 "
        "     AND f.created_at > COALESCE((SELECT MAX(e.created_at) FROM extractions e "
        "                                  WHERE e.document_id = d.id), '')) < ? "
        "ORDER BY d.id" + (" LIMIT ?" if limit is not None else ""),
        (PARSE_OK, MAX_FAILED_READS, *([limit] if limit is not None else [])),
    ).fetchall()
    return [r[0] for r in rows]


def review_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Documents waiting for a person: their current answer needs review, or every read failed.
    Oldest first. Each item has `reasons` (a list) and the current answer's fields, if any."""
    import json

    docs = conn.execute(
        "SELECT d.id FROM documents d WHERE d.parse_status = ? "
        "AND (d.source != 'local' "
        "     OR EXISTS (SELECT 1 FROM document_paths p WHERE p.document_id = d.id)) "
        "ORDER BY d.id",
        (PARSE_OK,),
    ).fetchall()
    queue = []
    for (doc_id,) in docs:
        row = current_extraction(conn, doc_id)
        if row is not None:
            if row["needs_review"] and not row["human_verified"]:
                item = {k: row[k] for k in _EXTRACTION_FIELDS}
                item.update(document_id=doc_id, extraction_id=row["id"],
                            reasons=json.loads(row["review_reasons"]))  # fmt: skip
                queue.append(item)
        elif failed_reads_since_last_answer(conn, doc_id) >= MAX_FAILED_READS:
            last = conn.execute(
                "SELECT error FROM extraction_failures WHERE document_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (doc_id,),
            ).fetchone()
            queue.append(
                {"document_id": doc_id, "extraction_id": None,
                 "reasons": [f"no answer from the model ({last['error']})"]}
            )  # fmt: skip
    return queue
