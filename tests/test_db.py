import sqlite3
from importlib import resources
from pathlib import Path

from paperclerk.db import connect, init_db, repo
from paperclerk.db.connection import MIGRATIONS


def _db():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def test_schema_is_idempotent():
    conn = connect(":memory:")
    init_db(conn)
    init_db(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"documents", "document_paths", "extractions", "subscriptions", "operations"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_upsert_dedupes_by_content_hash():
    conn = _db()
    first, new1 = repo.upsert_document(conn, sha256="abc", source="local", text="hi")
    second, new2 = repo.upsert_document(conn, sha256="abc", source="local")
    assert new1 and not new2
    assert first == second
    assert conn.execute("SELECT text FROM documents").fetchone()[0] == "hi"  # NULL keeps value


def test_a_path_points_at_one_document_and_documents_keep_their_content():
    conn = _db()
    oct_id, _ = repo.upsert_document(conn, sha256="oct", source="local", text="Oct $214")
    nov_id, _ = repo.upsert_document(conn, sha256="nov", source="local", text="Nov $198")
    repo.set_path(conn, "/in/invoice.txt", oct_id, mtime=1.0, size_bytes=8)
    repo.set_path(conn, "/in/invoice.txt", nov_id, mtime=2.0, size_bytes=8)

    assert repo.get_path(conn, "/in/invoice.txt")["document_id"] == nov_id
    texts = dict(conn.execute("SELECT id, text FROM documents").fetchall())
    assert texts == {oct_id: "Oct $214", nov_id: "Nov $198"}
    # The October bill is history: kept, but not listed as a current document.
    assert [(d["id"], d["path"]) for d in repo.list_documents(conn)] == [
        (nov_id, "/in/invoice.txt")
    ]


def test_paths_under_is_by_path_component_not_string_prefix():
    conn = _db()
    doc, _ = repo.upsert_document(conn, sha256="x", source="local")
    for p in ("/a/keep.txt", "/a/sub/b.txt", "/ab/other.txt"):
        repo.set_path(conn, p, doc, mtime=1.0, size_bytes=1)
    assert sorted(repo.paths_under(conn, Path("/a"))) == ["/a/keep.txt", "/a/sub/b.txt"]
    assert repo.delete_paths(conn, ["/a/keep.txt"]) == 1
    left = {r[0] for r in conn.execute("SELECT path FROM document_paths")}
    assert left == {"/a/sub/b.txt", "/ab/other.txt"}


def _old_db(path):
    """A database as created before any migration existed (main before this change)."""
    old = connect(path)
    schema = resources.files("paperclerk.db").joinpath("schema.sql").read_text(encoding="utf-8")
    old.executescript(schema)
    return old


def test_migrations_upgrade_existing_db_and_keep_rows(tmp_path):
    old = _old_db(tmp_path / "old.db")
    old.executemany(
        "INSERT INTO documents (sha256, path, source, mtime, size_bytes, text) "
        "VALUES (?, ?, 'local', ?, 5, ?)",
        [
            ("v1", "/in/bill.txt", 1.0, "stale"),  # old bug: two rows on one path
            ("v2", "/in/bill.txt", 2.0, "current"),
            ("pdf", "/in/a.pdf", 3.0, None),
        ],
    )
    old.commit()
    old.close()

    conn = connect(tmp_path / "old.db")
    init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    paths = {
        r["path"]: tuple(r)[1:]
        for r in conn.execute(
            "SELECT p.path, d.sha256, p.mtime, d.parse_status FROM document_paths p "
            "JOIN documents d ON d.id = p.document_id"
        )
    }
    assert paths == {"/in/bill.txt": ("v2", 2.0, None), "/in/a.pdf": ("pdf", 3.0, None)}
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3


def test_concurrent_migration_from_another_process_is_skipped(tmp_path):
    db = tmp_path / "old.db"
    _old_db(db).close()
    first, second = connect(db), connect(db)
    init_db(second)  # another process upgrades the database first
    init_db(first)  # must not re-run migrations (e.g. "duplicate column name")
    assert first.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert not first.in_transaction


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import paperclerk.db.connection as connection

    db = tmp_path / "t.db"
    conn = _old_db(db)
    broken = (("ALTER TABLE documents ADD COLUMN parse_status TEXT", "NOT VALID SQL"),)
    monkeypatch.setattr(connection, "MIGRATIONS", broken)
    try:
        connection.migrate(conn)
    except sqlite3.OperationalError:
        pass
    assert not conn.in_transaction
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert "parse_status" not in cols
