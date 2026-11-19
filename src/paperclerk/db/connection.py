from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

MEMORY = ":memory:"

# Schema changes applied on top of schema.sql, in order. Entry i (a tuple of statements) moves
# `PRAGMA user_version` from i to i + 1. Append only: never edit or reorder an entry that has
# shipped, because existing databases have already applied it.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 1: outcome of the last parse of a document's content ('ok' / 'unsupported' / 'failed';
    #    NULL = never recorded), so content is re-parsed once its parser lands.
    ("ALTER TABLE documents ADD COLUMN parse_status TEXT",),
    # 2: where each document currently lives on disk. One content (document) can sit at many
    #    paths, and one path can hold different content over time, so paths get their own table
    #    keyed by path. documents.path is no longer maintained for local files.
    (
        """CREATE TABLE document_paths (
            path        TEXT    PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            mtime       REAL    NOT NULL,          -- for incremental rescans
            size_bytes  INTEGER NOT NULL,
            seen_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX idx_document_paths_document ON document_paths(document_id)",
        # Carry over current local paths. If older code left several rows on one path, the
        # newest row wins; older ones keep their content and history but no path.
        """INSERT OR IGNORE INTO document_paths (path, document_id, mtime, size_bytes)
           SELECT path, id, COALESCE(mtime, 0), COALESCE(size_bytes, 0) FROM documents
           WHERE source = 'local' AND path IS NOT NULL ORDER BY id DESC""",
    ),
    # 3: text per page, with a per-page vision flag, replacing documents.text. Pages let
    #    answers cite a page and let F2 send only scanned pages to the vision model; a
    #    document's full text is its pages joined (repo.document_text). Existing text becomes
    #    page 1; images (no text layer) are flagged for vision.
    (
        """CREATE TABLE document_pages (
            document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_no      INTEGER NOT NULL CHECK (page_no >= 1),
            text         TEXT    NOT NULL,
            needs_vision INTEGER NOT NULL DEFAULT 0 CHECK (needs_vision IN (0, 1)),
            PRIMARY KEY (document_id, page_no)
        )""",
        """INSERT INTO document_pages (document_id, page_no, text, needs_vision)
           SELECT id, 1, COALESCE(text, ''), COALESCE(mime_type LIKE 'image/%', 0) FROM documents
           WHERE text IS NOT NULL OR parse_status = 'ok'""",
        "ALTER TABLE documents DROP COLUMN text",
    ),
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults. ':memory:' is allowed."""
    target = MEMORY
    if str(db_path) != MEMORY:
        p = Path(db_path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        target = str(p)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ALTER TABLE ... DROP COLUMN (migration 3) needs SQLite 3.35.
MIN_SQLITE = (3, 35, 0)


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist, then apply pending migrations. Safe on every startup."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"paperclerk needs SQLite {'.'.join(map(str, MIN_SQLITE))}+, "
            f"but Python is linked against {sqlite3.sqlite_version}"
        )
    schema = resources.files("paperclerk.db").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    migrate(conn)


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending MIGRATIONS, one transaction each.

    Safe when several processes (CLI, API server) open the same database at once: each step
    takes the write lock first (BEGIN IMMEDIATE) and re-reads the version under it, so a
    migration another process already applied is skipped instead of run twice.
    """
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= len(MIGRATIONS):
                conn.execute("COMMIT")
                return
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version + 1}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
