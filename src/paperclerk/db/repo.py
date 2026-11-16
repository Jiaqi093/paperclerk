"""Small data-access helpers. Keep SQL here, not in business logic."""

from __future__ import annotations

import sqlite3
from typing import Any


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
) -> tuple[int, bool]:
    """Insert a document, or update its path/mtime if the same content is already known.

    Returns (document_id, is_new).
    """
    row = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE documents SET path = COALESCE(?, path), mtime = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (path, mtime, row["id"]),
        )
        conn.commit()
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO documents "
        "(sha256, path, source, source_ref, mime_type, size_bytes, mtime, text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sha256, path, source, source_ref, mime_type, size_bytes, mtime, text),
    )
    conn.commit()
    return int(cur.lastrowid), True


def get_mtime_by_path(conn: sqlite3.Connection, path: str) -> float | None:
    row = conn.execute("SELECT mtime FROM documents WHERE path = ?", (path,)).fetchone()
    return row["mtime"] if row else None


def list_documents(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, path, source, mime_type, size_bytes, created_at FROM documents "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
