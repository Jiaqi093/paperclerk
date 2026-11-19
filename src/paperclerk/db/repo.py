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
) -> tuple[int, bool]:
    """Insert a document for this content, or fill in fields on the existing one.

    Never changes an existing document's sha256, source or source_ref; None arguments leave
    stored values alone (`pages=None` keeps the stored pages, a list replaces them;
    `parse_error` is replaced whenever `parse_status` is given, since it describes it). The row
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
                "parse_status = COALESCE(?, parse_status), updated_at = datetime('now') "
                "WHERE id = ?",
                (size_bytes, mime_type, parse_status, parse_error, parse_status, doc_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO documents "
                "(sha256, source, source_ref, mime_type, size_bytes, parse_status, parse_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sha256, source, source_ref, mime_type, size_bytes, parse_status, parse_error),
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
) -> None:
    """Record a new parse outcome; the document's pages become exactly `pages` (None: none)."""
    with conn:
        conn.execute(
            "UPDATE documents SET mime_type = COALESCE(?, mime_type), parse_status = ?, "
            "parse_error = ?, updated_at = datetime('now') WHERE id = ?",
            (mime_type, parse_status, parse_error, document_id),
        )
        _replace_pages(conn, document_id, pages or [])


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
        "SELECT p.document_id, p.mtime, p.size_bytes, d.sha256, d.parse_status "
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
    return [
        r["path"]
        for r in conn.execute("SELECT path FROM document_paths")
        if Path(r["path"]).is_relative_to(root)
    ]


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
        "d.created_at FROM documents d "
        "WHERE d.source != 'local' "
        "OR EXISTS (SELECT 1 FROM document_paths p WHERE p.document_id = d.id) "
        "ORDER BY d.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
