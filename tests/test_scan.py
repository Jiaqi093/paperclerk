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
