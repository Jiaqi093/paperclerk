from paperclerk.db import connect, init_db, repo


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
