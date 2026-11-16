from importlib import resources

from paperclerk.db import connect, init_db, repo
from paperclerk.db.connection import MIGRATIONS


def test_schema_is_idempotent():
    conn = connect(":memory:")
    init_db(conn)
    init_db(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"documents", "extractions", "subscriptions", "operations"} <= tables


def test_upsert_dedupes_by_content_hash():
    conn = connect(":memory:")
    init_db(conn)
    first, new1 = repo.upsert_document(conn, sha256="abc", path="/a/x.pdf", source="local")
    second, new2 = repo.upsert_document(conn, sha256="abc", path="/b/x.pdf", source="local")
    assert new1 and not new2
    assert first == second
    assert repo.list_documents(conn)[0]["path"] == "/b/x.pdf"


def test_upsert_edited_file_updates_row_in_place():
    conn = connect(":memory:")
    init_db(conn)
    first, _ = repo.upsert_document(
        conn, sha256="v1", path="/a/bill.txt", source="local", text="old"
    )
    second, is_new = repo.upsert_document(
        conn, sha256="v2", path="/a/bill.txt", source="local", text="new"
    )
    assert (second, is_new) == (first, False)
    rows = conn.execute("SELECT sha256, text FROM documents").fetchall()
    assert [tuple(r) for r in rows] == [("v2", "new")]


def test_upsert_detaches_path_when_file_now_matches_another_document():
    conn = connect(":memory:")
    init_db(conn)
    a, _ = repo.upsert_document(conn, sha256="aaa", path="/x/a.txt", source="local")
    b, _ = repo.upsert_document(conn, sha256="bbb", path="/x/b.txt", source="local")
    # b.txt is overwritten with a.txt's content: the document for that content now lives there.
    doc_id, _ = repo.upsert_document(conn, sha256="aaa", path="/x/b.txt", source="local")
    assert doc_id == a
    paths = dict(conn.execute("SELECT id, path FROM documents").fetchall())
    assert paths == {a: "/x/b.txt", b: None}


def test_migrations_bring_fresh_db_to_latest_version():
    conn = connect(":memory:")
    init_db(conn)
    init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert "parse_status" in cols


def test_migrations_upgrade_existing_db_without_losing_rows(tmp_path):
    db = tmp_path / "old.db"
    old = connect(db)
    schema = resources.files("paperclerk.db").joinpath("schema.sql").read_text(encoding="utf-8")
    old.executescript(schema)  # a database created before any migration existed
    old.execute("INSERT INTO documents (sha256, path, source) VALUES ('abc', '/a.pdf', 'local')")
    old.commit()
    old.close()

    conn = connect(db)
    init_db(conn)
    row = conn.execute("SELECT path, parse_status FROM documents").fetchone()
    assert tuple(row) == ("/a.pdf", None)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
