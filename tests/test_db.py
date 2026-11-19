import sqlite3
from importlib import resources
from pathlib import Path

from paperclerk.db import connect, init_db, repo
from paperclerk.db.connection import MIGRATIONS
from paperclerk.ingest.parsers import Page


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
    first, new1 = repo.upsert_document(conn, sha256="abc", source="local", pages=[Page("hi")])
    second, new2 = repo.upsert_document(conn, sha256="abc", source="local")
    assert new1 and not new2
    assert first == second
    assert repo.document_text(conn, first) == "hi"  # pages=None keeps the stored pages


def test_a_path_points_at_one_document_and_documents_keep_their_content():
    conn = _db()
    oct_id, _ = repo.upsert_document(conn, sha256="oct", source="local", pages=[Page("Oct $214")])
    nov_id, _ = repo.upsert_document(conn, sha256="nov", source="local", pages=[Page("Nov $198")])
    repo.set_path(conn, "/in/invoice.txt", oct_id, mtime=1.0, size_bytes=8)
    repo.set_path(conn, "/in/invoice.txt", nov_id, mtime=2.0, size_bytes=8)

    assert repo.get_path(conn, "/in/invoice.txt")["document_id"] == nov_id
    texts = {i: repo.document_text(conn, i) for i in (oct_id, nov_id)}
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
    # Text moved to page 1 (none for the never-parsed PDF), and the old column is gone.
    texts = {
        r["sha256"]: repo.document_text(conn, r["id"])
        for r in conn.execute("SELECT id, sha256 FROM documents")
    }
    assert texts == {"v1": "stale", "v2": "current", "pdf": None}
    assert "text" not in {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}


def _db_at(path, version):
    """A database migrated only up to `version`, as an older release left it."""
    conn = _old_db(path)
    for v in range(version):
        for statement in MIGRATIONS[v]:
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return conn


def test_migration_3_moves_text_to_pages_and_flags_images(tmp_path):
    old = _db_at(tmp_path / "v2.db", 2)
    old.executemany(
        "INSERT INTO documents (sha256, source, mime_type, text, parse_status) "
        "VALUES (?, 'local', ?, ?, ?)",
        [
            ("txt", "text/plain", "Water bill $40", "ok"),
            ("img", "image/png", "", "ok"),
            ("ok-no-mime", None, None, "ok"),
            ("pre-status", "text/plain", "old text", None),  # before parse_status existed
            ("unsupported", None, None, "unsupported"),
            ("failed", None, None, "failed"),
        ],
    )
    old.commit()
    old.close()

    conn = connect(tmp_path / "v2.db")
    init_db(conn)
    pages = {
        r["sha256"]: repo.get_pages(conn, r["id"])
        for r in conn.execute("SELECT id, sha256 FROM documents")
    }
    assert pages == {
        "txt": [Page("Water bill $40")],
        "img": [Page("", needs_vision=True)],
        "ok-no-mime": [Page("")],
        "pre-status": [Page("old text")],
        "unsupported": [],
        "failed": [],
    }


def test_set_parse_result_replaces_pages_and_none_clears_them():
    conn = _db()
    doc, _ = repo.upsert_document(
        conn, sha256="x", source="local", pages=[Page("a"), Page("", needs_vision=True)]
    )
    assert repo.document_text(conn, doc) == "a\n\n"
    repo.set_parse_result(conn, doc, pages=[Page("b")], mime_type=None, parse_status="ok")
    assert repo.get_pages(conn, doc) == [Page("b")]
    repo.set_parse_result(conn, doc, pages=None, mime_type=None, parse_status="failed")
    assert (repo.get_pages(conn, doc), repo.document_text(conn, doc)) == ([], None)


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


def test_upsert_with_pages_replaces_and_empty_clears():
    conn = _db()
    doc, _ = repo.upsert_document(conn, sha256="x", source="local", pages=[Page("a"), Page("b")])
    repo.upsert_document(conn, sha256="x", source="local", pages=[Page("c")])
    assert repo.get_pages(conn, doc) == [Page("c")]
    repo.upsert_document(conn, sha256="x", source="local", pages=[])
    assert repo.get_pages(conn, doc) == []


def test_document_row_and_pages_are_written_together(monkeypatch):
    conn = _db()

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(repo, "_replace_pages", boom)
    try:
        repo.upsert_document(conn, sha256="x", source="local", pages=[Page("a")])
    except sqlite3.OperationalError:
        pass
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_too_old_sqlite_fails_with_a_clear_message(monkeypatch):
    import pytest

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 31, 1))
    with pytest.raises(RuntimeError, match="needs SQLite 3.35"):
        init_db(connect(":memory:"))


def test_parse_error_follows_parse_status():
    conn = _db()
    doc, _ = repo.upsert_document(
        conn, sha256="x", source="local", parse_status="failed", parse_error="password protected"
    )
    repo.upsert_document(conn, sha256="x", source="local")  # no outcome given: keep it
    row = conn.execute("SELECT parse_status, parse_error FROM documents").fetchone()
    assert tuple(row) == ("failed", "password protected")
    repo.upsert_document(conn, sha256="x", source="local", parse_status="ok", pages=[Page("a")])
    row = conn.execute("SELECT parse_status, parse_error FROM documents").fetchone()
    assert tuple(row) == ("ok", None)


def test_no_cloud_downloads_sets_and_restores_the_thread_policy():
    import ctypes
    import sys

    import pytest

    from paperclerk.ingest import scanner

    if sys.platform != "darwin":
        pytest.skip("macOS only")
    libc = ctypes.CDLL(None)
    policy = (scanner._IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, scanner._IOPOL_SCOPE_THREAD)
    before = libc.getiopolicy_np(*policy)
    with scanner.no_cloud_downloads():
        assert libc.getiopolicy_np(*policy) == scanner._IOPOL_MATERIALIZE_DATALESS_FILES_OFF
    assert libc.getiopolicy_np(*policy) == before
