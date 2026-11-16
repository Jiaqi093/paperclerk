"""Small data-access helpers. Keep SQL here, not in business logic."""

from __future__ import annotations

import sqlite3
from typing import Any

# documents.parse_status values. NULL (rows from before migration 1) is treated as not parsed.
PARSE_OK = "ok"
PARSE_UNSUPPORTED = "unsupported"  # no parser implementation for this file type yet
PARSE_FAILED = "failed"  # the parser raised on this file


def upsert_document(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    path: str | None,
    source: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    mtime: float | None = None,
    text: str | None = None,
    source_ref: str | None = None,
    parse_status: str | None = None,
) -> tuple[int, bool]:
    """Record a file's current content and keep at most one document row per path.

    - Content already known (same sha256): update that row's path and fields. Moving or
      copying a file never creates a new document.
    - New content at a path we already index (the file was edited): update that row in place,
      so the document keeps its id and the old text doesn't linger in a second row.
    - Otherwise insert a new row.

    Returns (document_id, is_new).
    """
    by_sha = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
    by_path = None
    if path is not None:
        by_path = conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()

    if by_sha is not None:
        doc_id, is_new = by_sha["id"], False
        conn.execute(
            "UPDATE documents SET path = COALESCE(?, path), mtime = ?, "
            "size_bytes = COALESCE(?, size_bytes), mime_type = COALESCE(?, mime_type), "
            "text = COALESCE(?, text), parse_status = COALESCE(?, parse_status), "
            "updated_at = datetime('now') WHERE id = ?",
            (path, mtime, size_bytes, mime_type, text, parse_status, doc_id),
        )
    elif by_path is not None:
        doc_id, is_new = by_path["id"], False
        conn.execute(
            "UPDATE documents SET sha256 = ?, mtime = ?, size_bytes = ?, mime_type = ?, "
            "text = ?, parse_status = ?, updated_at = datetime('now') WHERE id = ?",
            (sha256, mtime, size_bytes, mime_type, text, parse_status, doc_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO documents "
            "(sha256, path, source, source_ref, mime_type, size_bytes, mtime, text, parse_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sha256, path, source, source_ref, mime_type, size_bytes, mtime, text, parse_status),
        )
        doc_id, is_new = int(cur.lastrowid), True

    if by_path is not None and by_path["id"] != doc_id:
        # The file at this path now holds another document's content: the old row's content
        # is no longer at this path. Detach it rather than delete it (it may have extractions).
        conn.execute(
            "UPDATE documents SET path = NULL, updated_at = datetime('now') WHERE id = ?",
            (by_path["id"],),
        )
    conn.commit()
    return doc_id, is_new


def is_up_to_date(conn: sqlite3.Connection, path: str, mtime: float) -> bool:
    """True when the file at `path` was indexed at this mtime and parsed successfully.

    Anything else (unknown path, changed mtime, parser missing or failed last time) means
    the scanner should process the file again.
    """
    row = conn.execute(
        "SELECT mtime, parse_status FROM documents WHERE path = ?", (path,)
    ).fetchone()
    return row is not None and row["mtime"] == mtime and row["parse_status"] == PARSE_OK


def list_documents(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, path, source, mime_type, size_bytes, created_at FROM documents "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
