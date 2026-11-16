from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

MEMORY = ":memory:"

# Schema changes applied on top of schema.sql, in order. Entry i moves `PRAGMA user_version`
# from i to i + 1. Append only: never edit or reorder an entry that has shipped, because
# existing databases have already applied it.
MIGRATIONS: tuple[str, ...] = (
    # 1: outcome of the last parse ('ok' / 'unsupported' / 'failed'; NULL = never recorded),
    #    so files are re-parsed once a parser lands or a parser bug is fixed.
    "ALTER TABLE documents ADD COLUMN parse_status TEXT",
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


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist, then apply pending migrations. Safe on every startup."""
    schema = resources.files("paperclerk.db").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        # One transaction per migration: the change and the version bump land together.
        conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {target}; COMMIT;")
    conn.commit()
