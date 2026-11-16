"""Small data-access helpers. Keep SQL here, not in business logic.

Model: a *document* is one unique content (sha256) and never changes identity; its text and
parse outcome belong to that content. *document_paths* records where each content currently
sits on disk: one document can have many paths (copies), a path holds exactly one document,
and when a file's content changes its path simply points at a different document.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

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
    text: str | None = None,
    source_ref: str | None = None,
    parse_status: str | None = None,
) -> tuple[int, bool]:
    """Insert a document for this content, or fill in fields on the existing one.

    Never changes an existing document's sha256, source or source_ref; NULL arguments leave
    stored values alone. Returns (document_id, is_new).
    """
    row = get_document_by_sha(conn, sha256)
    if row is not None:
        conn.execute(
            "UPDATE documents SET size_bytes = COALESCE(?, size_bytes), "
            "mime_type = COALESCE(?, mime_type), text = COALESCE(?, text), "
            "parse_status = COALESCE(?, parse_status), updated_at = datetime('now') "
            "WHERE id = ?",
            (size_bytes, mime_type, text, parse_status, row["id"]),
        )
        conn.commit()
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO documents "
        "(sha256, source, source_ref, mime_type, size_bytes, text, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sha256, source, source_ref, mime_type, size_bytes, text, parse_status),
    )
    conn.commit()
    return int(cur.lastrowid), True


def set_parse_result(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    text: str | None,
    mime_type: str | None,
    parse_status: str,
) -> None:
    conn.execute(
        "UPDATE documents SET text = ?, mime_type = COALESCE(?, mime_type), parse_status = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (text, mime_type, parse_status, document_id),
    )
    conn.commit()


def get_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    """What the database knows about the file at `path`: its document, mtime, size, parse state."""
    return conn.execute(
        "SELECT p.document_id, p.mtime, p.size_bytes, d.parse_status "
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
        "AS path, d.source, d.mime_type, d.size_bytes, d.created_at FROM documents d "
        "WHERE d.source != 'local' "
        "OR EXISTS (SELECT 1 FROM document_paths p WHERE p.document_id = d.id) "
        "ORDER BY d.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
