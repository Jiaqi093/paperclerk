from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

MEMORY = ":memory:"


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
    """Create tables if they don't exist. Safe to call on every startup."""
    schema = resources.files("paperclerk.db").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.commit()
