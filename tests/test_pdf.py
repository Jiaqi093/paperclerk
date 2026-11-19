"""PDF parsing, on PDFs generated with reportlab (so no real documents are needed)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from paperclerk.ingest.parsers import Page, ParseError
from paperclerk.ingest.parsers.pdf import PdfParser

BILL = "Origin Energy electricity bill. Amount due $214.37 by 24 Nov 2026."
STAMP = "Scanned by CamScanner 2026-10-24 10:31"  # > MIN_CHARS_PER_PAGE on its own
W, H = A4


def _parse(path: Path):
    return PdfParser().parse(path.read_bytes(), path)


def _photo(tmp_path: Path) -> str:
    p = tmp_path / "photo.png"
    Image.new("RGB", (60, 90), "white").save(p)
    return str(p)


def _pdf(path: Path, pages: list[dict], **kwargs) -> Path:
    """Each page dict may have `lines` (text lines) and `image` (a full-page picture)."""
    c = canvas.Canvas(str(path), pagesize=A4, **kwargs)
    for page in pages:
        if "image" in page:
            c.drawImage(page["image"], 0, 0, W, H)
        for i, line in enumerate(page.get("lines", [])):
            c.drawString(72, H - 72 - 14 * i, line)
        c.showPage()
    c.save()
    return path


def test_text_pdf_gives_text_per_page(tmp_path):
    pdf = _pdf(tmp_path / "bill.pdf", [{"lines": [BILL]}, {"lines": ["Usage: 320 kWh peak"] * 2}])
    doc = _parse(pdf)
    assert doc.mime_type == "application/pdf"
    assert doc.pages == (Page(BILL), Page("Usage: 320 kWh peak\nUsage: 320 kWh peak"))
    assert not doc.needs_vision


def test_photo_without_text_is_flagged_for_vision(tmp_path):
    doc = _parse(_pdf(tmp_path / "scan.pdf", [{"image": _photo(tmp_path)}]))
    assert doc.pages == (Page("", needs_vision=True),)


def test_nearly_empty_page_is_not_sent_to_vision(tmp_path):
    # Like the last page of a real airline e-ticket: one link in a frame, nothing else.
    path = tmp_path / "ticket.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.rect(36, H - 80, W - 72, 40)
    c.drawString(W / 2 - 60, H - 64, "Important Legal Notices")
    c.showPage()
    c.save()
    assert _parse(path).pages == (Page("Important Legal Notices"),)
    assert _parse(_pdf(tmp_path / "blank.pdf", [{}])).pages == (Page(""),)


def test_text_drawn_as_outlines_is_flagged_for_vision(tmp_path):
    # Some generators convert text to vector shapes: no text layer, but plenty to read.
    path = tmp_path / "outlined.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    for i in range(300):
        c.rect(40 + (i % 30) * 17, 700 - (i // 30) * 20, 10, 12, fill=1)
    c.showPage()
    c.save()
    assert _parse(path).needs_vision


def test_photo_with_a_stamped_line_is_a_scan_but_only_that_page(tmp_path):
    photo = _photo(tmp_path)
    pdf = _pdf(tmp_path / "mixed.pdf", [{"lines": [BILL]}, {"image": photo, "lines": [STAMP]}])
    doc = _parse(pdf)
    assert [p.needs_vision for p in doc.pages] == [False, True]
    assert doc.pages[1].text == STAMP  # the text layer is still kept


def test_full_text_page_over_a_background_image_is_not_a_scan(tmp_path):
    photo = _photo(tmp_path)
    pdf = _pdf(tmp_path / "letterhead.pdf", [{"image": photo, "lines": [BILL] * 5}])
    assert not _parse(pdf).needs_vision


def test_password_protected_pdf_says_so(tmp_path):
    pdf = _pdf(tmp_path / "statement.pdf", [{"lines": [BILL]}], encrypt="secret")
    with pytest.raises(ParseError, match="password protected"):
        _parse(pdf)


def test_damaged_pdf_raises_parse_error(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(ParseError, match="damaged"):
        _parse(bad)


def test_scan_indexes_pdfs_and_reports_why_one_failed(clerk, inbox):
    from paperclerk.db import repo

    _pdf(inbox / "bill.pdf", [{"lines": [BILL]}, {"image": _photo(inbox.parent)}])
    _pdf(inbox / "locked.pdf", [{"lines": [BILL]}], encrypt="secret")
    r = clerk.scan()
    assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["locked.pdf"]

    docs = {d["path"].rsplit("/", 1)[-1]: d for d in clerk.list_documents()}
    assert (docs["bill.pdf"]["parse_status"], docs["bill.pdf"]["parse_error"]) == ("ok", None)
    assert repo.get_pages(clerk.conn, docs["bill.pdf"]["id"]) == [
        Page(BILL),
        Page("", needs_vision=True),
    ]
    assert (docs["locked.pdf"]["parse_status"], docs["locked.pdf"]["parse_error"]) == (
        "failed",
        "password protected",
    )


def _form_pdf(path: Path, photo: str, form_size: tuple[float, float], scale: float) -> Path:
    """A page whose picture sits inside a form (reusable content block), scaled onto the page."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.beginForm("scan")
    c.drawImage(photo, 0, 0, *form_size)
    c.endForm()
    c.saveState()
    c.transform(scale, 0, 0, scale, 0, 0)
    c.doForm("scan")
    c.restoreState()
    c.drawString(72, H - 72, STAMP)
    c.showPage()
    c.save()
    return path


def test_picture_inside_a_scaled_up_form_counts_at_its_page_size(tmp_path):
    # Drawn 100x100 in the form, scaled to fill the page.
    pdf = _form_pdf(tmp_path / "f.pdf", _photo(tmp_path), (W / 6, H / 6), 6)
    assert _parse(pdf).needs_vision


def test_picture_inside_a_scaled_down_form_is_small(tmp_path):
    # Full page size in the form, shrunk to 10% on the page: a thumbnail, not a scan.
    pdf = _form_pdf(tmp_path / "f.pdf", _photo(tmp_path), (W, H), 0.1)
    assert not _parse(pdf).needs_vision


def test_one_unreadable_page_keeps_the_others(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    from paperclerk.ingest.parsers import pdf as pdf_mod

    real = pdf_mod._read_page
    calls = []

    def flaky(page):
        calls.append(1)
        if len(calls) == 2:
            raise pdfium.PdfiumError("Failed to load page.")
        return real(page)

    monkeypatch.setattr(pdf_mod, "_read_page", flaky)
    pdf = _pdf(tmp_path / "bill.pdf", [{"lines": [BILL]}, {"lines": [BILL]}, {"lines": [BILL]}])
    doc = _parse(pdf)
    assert doc.pages == (Page(BILL), Page("", needs_vision=True), Page(BILL))


def test_unlocked_copy_saved_over_a_locked_pdf_is_ok(clerk, inbox):
    import os

    locked = _pdf(inbox / "statement.pdf", [{"lines": [BILL]}], encrypt="secret")
    clerk.scan()
    _pdf(locked, [{"lines": [BILL]}])  # the user saves an unlocked copy over it
    st = locked.stat()
    os.utime(locked, (st.st_atime, st.st_mtime + 10))
    clerk.scan()
    doc = next(d for d in clerk.list_documents() if d["path"].endswith("statement.pdf"))
    assert (doc["parse_status"], doc["parse_error"]) == ("ok", None)


def test_unreadable_pdf_is_retried_not_recorded(clerk, inbox):
    pdf = _pdf(inbox / "bill.pdf", [{"lines": [BILL]}])
    pdf.chmod(0)
    try:
        r = clerk.scan()
        assert [p.rsplit("/", 1)[-1] for p in r.failed] == ["bill.pdf"]
        assert not any(d["path"].endswith("bill.pdf") for d in clerk.list_documents())
    finally:
        pdf.chmod(0o644)
    clerk.scan()
    assert any(d["path"].endswith("bill.pdf") for d in clerk.list_documents())


def test_card_sized_scan_on_a_page_is_flagged(tmp_path):
    # An 85x55 mm card scanned onto A4 covers about 7.5% of the page.
    path = tmp_path / "card.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawImage(_photo(tmp_path), 72, 500, 241, 156)
    c.showPage()
    c.save()
    assert _parse(path).needs_vision


def test_hyphen_across_a_line_break_is_kept_as_a_hyphen(tmp_path):
    pdf = _pdf(tmp_path / "t.pdf", [{"lines": ["Taxes/Fees/Carrier-", "Imposed Charges $12.40"]}])
    assert "\ufffe" not in _parse(pdf).text
    assert "Carrier-Imposed" in _parse(pdf).text
