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


def _bump_mtime(path):
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def _rows(clerk):
    return [dict(r) for r in clerk.conn.execute("SELECT path, text, parse_status FROM documents")]


def test_edited_file_keeps_one_row_and_settles(clerk, inbox):
    clerk.scan()
    note = inbox / "note.txt"
    note.write_text("Electricity bill due 24 Nov, $198.10", encoding="utf-8")
    _bump_mtime(note)

    r = clerk.scan()
    assert (r.seen, r.new, r.updated) == (1, 0, 1)
    note_rows = [row for row in _rows(clerk) if row["path"].endswith("note.txt")]
    assert [row["text"] for row in note_rows] == ["Electricity bill due 24 Nov, $198.10"]
    assert clerk.scan().seen == 0  # no longer treated as changed on every scan


def test_file_is_reparsed_once_its_parser_lands(clerk, inbox, monkeypatch):
    def not_yet(self, path):
        raise NotImplementedError

    monkeypatch.setattr(TextParser, "parse", not_yet)
    r = clerk.scan()
    assert any(p.endswith("note.txt") for p in r.skipped)
    monkeypatch.undo()  # the parser "lands"; the file itself is unchanged

    r = clerk.scan()
    assert r.seen == 1 and not r.skipped
    note = next(row for row in _rows(clerk) if row["path"].endswith("note.txt"))
    assert (note["text"], note["parse_status"]) == ("Electricity bill due 24 Oct, $214.37", "ok")
    assert clerk.scan().seen == 0


def test_one_broken_file_does_not_stop_the_scan(clerk, inbox, monkeypatch):
    (inbox / "broken.txt").write_text("garbage", encoding="utf-8")
    real_parse = TextParser.parse

    def flaky(self, path):
        if path.name == "broken.txt":
            raise ValueError("malformed file")
        return real_parse(self, path)

    monkeypatch.setattr(TextParser, "parse", flaky)
    r = clerk.scan()
    assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["broken.txt"]
    assert r.new == 3  # note.txt and receipt.png are still indexed
    broken = next(row for row in _rows(clerk) if row["path"].endswith("broken.txt"))
    assert broken["parse_status"] == "failed"

    r = clerk.scan()  # only the failed file is retried
    assert (r.seen, len(r.failed)) == (1, 1)
