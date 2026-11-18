import os

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
    return [r[0] for r in clerk.conn.execute("SELECT text FROM documents ORDER BY id")]


def _path_texts(clerk):
    rows = clerk.conn.execute(
        "SELECT p.path, d.text FROM document_paths p JOIN documents d ON d.id = p.document_id"
    )
    return {r[0].rsplit("/", 1)[-1]: r[1] for r in rows}


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
    def not_yet(self, path):
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

    def flaky(self, path):
        if path.name == "broken.txt":
            raise ValueError("malformed file")
        return real_parse(self, path)

    monkeypatch.setattr(TextParser, "parse", flaky)
    r = clerk.scan()
    assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["broken.txt"]
    assert r.new == 3  # note.txt and receipt.png are still indexed

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
    real_parse = TextParser.parse

    def locked(self, path):
        raise PermissionError(path)

    monkeypatch.setattr(TextParser, "parse", locked)
    r = clerk.scan()
    assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["note.txt"]
    assert "note.txt" not in _path_texts(clerk)  # nothing recorded: retried next scan

    monkeypatch.setattr(TextParser, "parse", real_parse)
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
    def not_yet(self, path):
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
