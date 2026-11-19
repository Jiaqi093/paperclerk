import os

from paperclerk.db import repo
from paperclerk.ingest.parsers.text import TextParser


def test_scan_indexes_supported_files_and_skips_hidden(clerk):
    r = clerk.scan()
    paths = sorted(d["path"].rsplit("/", 1)[-1] for d in clerk.list_documents())
    assert paths == ["note.txt", "receipt.png"]
    assert r.new == 2


def test_rescan_is_incremental(clerk, inbox):
    clerk.scan()
    assert clerk.scan().seen == 0  # nothing changed

    (inbox / "new.md").write_text("# new doc", encoding="utf-8")
    r = clerk.scan()
    assert (r.seen, r.new) == (1, 1)


def _counts(r):
    return (r.seen, r.new, r.updated, r.removed)


def _bump_mtime(path, by=10):
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + by))


def _texts(clerk):
    ids = [r[0] for r in clerk.conn.execute("SELECT id FROM documents ORDER BY id")]
    return [repo.document_text(clerk.conn, i) for i in ids]


def _path_texts(clerk):
    rows = clerk.conn.execute("SELECT path, document_id FROM document_paths")
    return {r[0].rsplit("/", 1)[-1]: repo.document_text(clerk.conn, r[1]) for r in rows}


def test_new_content_at_same_path_is_a_new_document_and_old_text_is_kept(clerk, inbox):
    clerk.scan()
    note = inbox / "note.txt"
    note.write_text("Electricity bill due 24 Nov, $198.10", encoding="utf-8")
    _bump_mtime(note)

    assert _counts(clerk.scan()) == (1, 1, 0, 0)
    assert _path_texts(clerk)["note.txt"] == "Electricity bill due 24 Nov, $198.10"
    assert "Electricity bill due 24 Oct, $214.37" in _texts(clerk)  # October bill not lost
    assert clerk.scan().seen == 0  # settles


def test_copies_share_one_document_and_settle(clerk, inbox):
    clerk.scan()
    (inbox / "sub").mkdir()
    (inbox / "sub" / "note-copy.txt").write_bytes((inbox / "note.txt").read_bytes())

    assert _counts(clerk.scan()) == (1, 0, 1, 0)
    assert len(_texts(clerk)) == 2  # note + receipt, no new document for the copy
    assert set(_path_texts(clerk)) == {"note.txt", "note-copy.txt", "receipt.png"}
    assert _counts(clerk.scan()) == (0, 0, 0, 0)


def test_overwrite_with_other_content_then_settle(clerk, inbox):
    (inbox / "other.txt").write_text("Water bill $40", encoding="utf-8")
    clerk.scan()
    other = inbox / "other.txt"
    other.write_bytes((inbox / "note.txt").read_bytes())
    _bump_mtime(other)

    assert _counts(clerk.scan()) == (1, 0, 1, 0)
    assert _path_texts(clerk)["other.txt"] == "Electricity bill due 24 Oct, $214.37"
    assert "Water bill $40" in _texts(clerk)
    assert _counts(clerk.scan()) == (0, 0, 0, 0)


def test_deleted_and_moved_files(clerk, inbox):
    clerk.scan()
    (inbox / "receipt.png").unlink()
    (inbox / "note.txt").rename(inbox / "renamed.txt")

    assert _counts(clerk.scan()) == (1, 0, 1, 2)
    assert set(_path_texts(clerk)) == {"renamed.txt"}
    assert len(_texts(clerk)) == 2  # documents are kept even when no file holds them


def test_missing_watched_folder_keeps_its_paths(clerk, inbox, tmp_path):
    clerk.scan()
    inbox.rename(tmp_path / "unplugged")
    assert clerk.scan().removed == 0
    assert len(_path_texts(clerk)) == 2


def test_file_is_reparsed_once_its_parser_lands(clerk, inbox, monkeypatch):
    def not_yet(self, data, path):
        raise NotImplementedError

    monkeypatch.setattr(TextParser, "parse", not_yet)
    r = clerk.scan()
    assert any(p.endswith("note.txt") for p in r.skipped)
    r = clerk.scan()  # still no parser: reported, but not counted as work
    assert (r.seen, r.updated, len(r.skipped)) == (0, 0, 1)

    monkeypatch.undo()  # the parser "lands"; the file itself is unchanged
    r = clerk.scan()
    assert (r.seen, r.updated, r.skipped) == (1, 1, [])
    assert _path_texts(clerk)["note.txt"] == "Electricity bill due 24 Oct, $214.37"
    assert clerk.scan().seen == 0


def test_one_broken_file_does_not_stop_the_scan(clerk, inbox, monkeypatch):
    broken = inbox / "broken.txt"
    broken.write_text("garbage", encoding="utf-8")
    real_parse = TextParser.parse

    def flaky(self, data, path):
        if path.name == "broken.txt":
            raise ValueError("malformed file")
        return real_parse(self, data, path)

    monkeypatch.setattr(TextParser, "parse", flaky)
    r = clerk.scan()
    assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["broken.txt"]
    assert r.new == 3  # note.txt and receipt.png are still indexed
    errors = {d["path"].rsplit("/", 1)[-1]: d["parse_error"] for d in clerk.list_documents()}
    assert errors["broken.txt"] == "damaged or unreadable file"

    assert clerk.scan().seen == 0  # not retried while the file is unchanged
    broken.write_text("garbage, fixed", encoding="utf-8")
    _bump_mtime(broken)
    r = clerk.scan()
    assert (r.seen, len(r.failed)) == (1, 1)  # retried once it changes


def test_file_vanishing_mid_scan_does_not_stop_it(clerk, inbox, monkeypatch):
    from pathlib import Path

    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "note.txt":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    r = clerk.scan()
    assert r.new == 1  # receipt.png


def test_overlapping_watched_folders_settle(inbox, tmp_path):
    from paperclerk.config import Settings
    from paperclerk.core import PaperClerk
    from paperclerk.db import connect

    # inbox/.hidden is skipped when walking inbox, but is itself a watched folder.
    clerk = PaperClerk(
        Settings(watched_folders=[inbox, inbox / ".hidden"]), conn=connect(":memory:")
    )
    clerk.scan()
    assert _counts(clerk.scan()) == (0, 0, 0, 0)
    assert "secret.txt" in _path_texts(clerk)


def test_transient_read_error_is_not_recorded_as_failed_content(clerk, inbox, monkeypatch):
    from pathlib import Path

    real_open = Path.open

    def locked(self, mode="r", *args, **kwargs):
        if self.name == "note.txt" and mode == "rb":
            raise PermissionError(self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    r = clerk.scan()
    assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["note.txt"]
    assert "note.txt" not in _path_texts(clerk)  # nothing recorded: retried next scan

    monkeypatch.undo()
    r = clerk.scan()
    assert (r.new, r.failed) == (1, [])
    assert _path_texts(clerk)["note.txt"] == "Electricity bill due 24 Oct, $214.37"


def test_list_documents_hides_content_no_longer_on_disk(clerk, inbox):
    clerk.scan()
    (inbox / "receipt.png").unlink()
    clerk.scan()
    assert [d["path"].rsplit("/", 1)[-1] for d in clerk.list_documents()] == ["note.txt"]


def _keep_mtime_write(path, data):
    st = path.stat()
    path.write_bytes(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_reparse_of_unchanged_looking_file_does_not_rewrite_shared_copy(clerk, inbox, monkeypatch):
    def not_yet(self, data, path):
        raise NotImplementedError

    (inbox / "copy.txt").write_bytes((inbox / "note.txt").read_bytes())
    monkeypatch.setattr(TextParser, "parse", not_yet)
    clerk.scan()  # note.txt and copy.txt share one unparsed document
    # Same size, mtime preserved (`cp -p`, rsync -t): looks unchanged, but the bytes differ.
    # (copy.txt is scanned first, so its new text must not land on the shared document.)
    _keep_mtime_write(inbox / "copy.txt", b"Electricity bill due 24 Oct, $999.99")

    monkeypatch.undo()  # the parser lands
    clerk.scan()
    texts = _path_texts(clerk)
    assert texts["note.txt"] == "Electricity bill due 24 Oct, $214.37"
    assert texts["copy.txt"] == "Electricity bill due 24 Oct, $999.99"
    assert clerk.scan().seen == 0


def test_unreadable_folder_is_reported_not_forgotten(clerk, inbox):
    sub = inbox / "sub"
    sub.mkdir()
    (sub / "bill.txt").write_text("Water bill", encoding="utf-8")
    clerk.scan()
    sub.chmod(0)
    try:
        r = clerk.scan()
        assert r.removed == 0
        assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["sub"]
        assert "bill.txt" in [d["path"].rsplit("/", 1)[-1] for d in clerk.list_documents()]
    finally:
        sub.chmod(0o755)
    assert _counts(clerk.scan()) == (0, 0, 0, 0)


def test_unstatable_file_is_reported_not_forgotten(clerk, inbox, monkeypatch):
    from pathlib import Path

    clerk.scan()
    real_stat = Path.stat

    def denied(self, *args, **kwargs):
        if self.name == "note.txt":
            raise PermissionError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    r = clerk.scan()
    assert (r.removed, [p.rsplit("/", 1)[-1] for p in r.failed]) == (0, ["note.txt"])
    assert "note.txt" in _path_texts(clerk)


def test_unreadable_watched_folder_keeps_all_its_paths(clerk, inbox):
    clerk.scan()
    inbox.chmod(0)
    try:
        r = clerk.scan()
        assert (r.removed, [p.rsplit("/", 1)[-1] for p in r.failed]) == (0, ["inbox"])
    finally:
        inbox.chmod(0o755)
    assert len(clerk.list_documents()) == 2


def test_symlink_loop_is_ignored_quietly(clerk, inbox):
    (inbox / "loop.txt").symlink_to(inbox / "loop.txt")
    r = clerk.scan()
    assert (r.new, r.failed) == (2, [])


def _pages_of(clerk, name):
    doc_id = next(
        r[1]
        for r in clerk.conn.execute("SELECT path, document_id FROM document_paths")
        if r[0].endswith("/" + name)
    )
    return repo.get_pages(clerk.conn, doc_id)


def test_scan_stores_pages_and_flags_images_for_vision(clerk):
    from paperclerk.ingest.parsers import Page

    clerk.scan()
    assert _pages_of(clerk, "note.txt") == [Page("Electricity bill due 24 Oct, $214.37")]
    assert _pages_of(clerk, "receipt.png") == [Page("", needs_vision=True)]


def test_reparse_replaces_pages_instead_of_appending(clerk, inbox, monkeypatch):
    from paperclerk.ingest.parsers import Page, ParsedDocument

    clerk.scan()
    two_pages = ParsedDocument("text/plain", (Page("page one"), Page("page two")))
    monkeypatch.setattr(TextParser, "parse", lambda self, data, path: two_pages)
    note = inbox / "note.txt"
    note.write_text("changed", encoding="utf-8")
    _bump_mtime(note)
    clerk.scan()
    assert _pages_of(clerk, "note.txt") == [Page("page one"), Page("page two")]
    assert _path_texts(clerk)["note.txt"] == "page one\n\npage two"


def test_failed_reparse_clears_old_pages(clerk, inbox, monkeypatch):
    clerk.scan()
    doc_id = clerk.conn.execute(
        "SELECT document_id FROM document_paths WHERE path LIKE '%note.txt'"
    ).fetchone()[0]
    # A document from before parse_status existed: old text, outcome never recorded.
    clerk.conn.execute("UPDATE documents SET parse_status = NULL WHERE id = ?", (doc_id,))
    clerk.conn.execute("DELETE FROM document_paths WHERE document_id = ?", (doc_id,))
    clerk.conn.commit()

    def broken(self, data, path):
        raise ValueError("malformed")

    monkeypatch.setattr(TextParser, "parse", broken)
    clerk.scan()  # same content seen again, now unparseable
    row = clerk.conn.execute("SELECT parse_status FROM documents WHERE id = ?", (doc_id,))
    assert row.fetchone()[0] == "failed"
    assert repo.get_pages(clerk.conn, doc_id) == []  # no stale text next to "failed"


def test_hash_and_text_come_from_the_same_bytes(clerk, inbox, monkeypatch):
    import hashlib

    note = inbox / "note.txt"
    october = note.read_bytes()
    real_parse = TextParser.parse

    def overwritten_meanwhile(self, data, path):
        # The user saves November's bill over the file after it was read, before it's parsed.
        if path.name == "note.txt":
            path.write_text("Electricity bill due 24 Nov, $198.10", encoding="utf-8")
            _bump_mtime(path)
        return real_parse(self, data, path)

    monkeypatch.setattr(TextParser, "parse", overwritten_meanwhile)
    clerk.scan()
    monkeypatch.undo()
    row = clerk.conn.execute(
        "SELECT id FROM documents WHERE sha256 = ?", (hashlib.sha256(october).hexdigest(),)
    ).fetchone()
    assert repo.document_text(clerk.conn, row[0]) == october.decode()  # October's hash, text

    clerk.scan()  # the new mtime is noticed: November is indexed too
    assert _path_texts(clerk)["note.txt"] == "Electricity bill due 24 Nov, $198.10"


def _write_on_open(monkeypatch, name, times):
    """Make the scan's read of `name` race a writer: every time the scan opens it (up to
    `times`), the file is rewritten with longer content right after."""
    from pathlib import Path

    real_open = Path.open
    writes = []

    def racing_open(self, mode="r", *args, **kwargs):
        f = real_open(self, mode, *args, **kwargs)
        if self.name == name and mode == "rb" and len(writes) < times:
            writes.append(1)
            with open(self, "a", encoding="utf-8") as w:  # builtins.open: not patched
                w.write(" (saving)")
        return f

    monkeypatch.setattr(Path, "open", racing_open)


def test_file_written_once_while_being_read_is_read_again(clerk, inbox, monkeypatch):
    _write_on_open(monkeypatch, "note.txt", times=1)  # e.g. a cloud-sync download
    r = clerk.scan()
    monkeypatch.undo()
    assert r.failed == []
    assert _path_texts(clerk)["note.txt"].endswith("(saving)")
    assert clerk.scan().seen == 0  # the stored mtime and size are the re-read's: settled


def test_file_that_keeps_changing_is_retried_next_scan(clerk, inbox, monkeypatch):
    _write_on_open(monkeypatch, "note.txt", times=2)
    r = clerk.scan()
    monkeypatch.undo()
    assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["note.txt"]
    assert "note.txt" not in _path_texts(clerk)  # nothing recorded
    assert clerk.scan().failed == []
    assert _path_texts(clerk)["note.txt"].endswith("(saving) (saving)")


def test_huge_file_is_hashed_but_not_parsed(clerk, inbox, monkeypatch):
    from paperclerk.core import service

    monkeypatch.setattr(service, "MAX_PARSE_BYTES", 10)
    (inbox / "copy.txt").write_bytes((inbox / "note.txt").read_bytes())
    r = clerk.scan()
    # copy.txt is scanned first; note.txt then reuses its (failed) document without parsing.
    assert sorted(p.rsplit("/", 1)[-1] for p in r.failed) == ["copy.txt", "receipt.png"]
    rows = clerk.conn.execute(
        "SELECT p.path, d.id, d.parse_error FROM document_paths p "
        "JOIN documents d ON d.id = p.document_id"
    )
    docs = {r[0].rsplit("/", 1)[-1]: (r[1], r[2]) for r in rows}
    assert docs["note.txt"] == docs["copy.txt"]  # still deduplicated by hash
    assert docs["note.txt"][1].startswith("too large to read")
