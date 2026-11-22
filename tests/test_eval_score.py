import json
from datetime import date

import pytest

from paperclerk.evals.data import DataError, Label, Prediction, load_labels, read_texts
from paperclerk.evals.judge import Outcome, judge_field, key_fields
from paperclerk.evals.report import load_history, previous_run, render, save_run, verdict
from paperclerk.evals.score import score
from paperclerk.extract.schemas import DocType, Extraction

BILL = "Origin Energy\nThis period $214.37\nOverdue $50.00\nTotal due $264.37\nDue 24 Oct 2026"
PROMO = "Big Sale at Harvey Norman! Was $99, now $49.99. Ends 30 Nov 2026."


def bill_label(**kw) -> Label:
    values = dict(
        file="bill.txt",
        doc_type="invoice",
        vendor="Origin Energy",
        amount=264.37,
        currency="AUD",
        due_date="2026-10-24",
    )
    return Label.model_validate({**values, **kw})


def answer(file: str, confidence: float = 0.95, **kw) -> Prediction:
    out = Extraction(doc_type=DocType.INVOICE, confidence=confidence, **kw)
    return Prediction(file=file, output=out, cost_usd=0.002, latency_ms=1200)


GOOD = dict(vendor="Origin Energy Pty Ltd", amount=264.37, currency="aud", due_date="2026-10-24")


# --- one field ---------------------------------------------------------------------------


def test_field_outcomes():
    assert judge_field("amount", 264.37, 264.375, BILL) is Outcome.CORRECT  # within a cent
    assert judge_field("amount", 264.37, None, BILL) is Outcome.MISSED
    assert judge_field("amount", 264.37, 214.37, BILL) is Outcome.WRONG  # on the bill
    assert judge_field("amount", 264.37, 246.37, BILL) is Outcome.FABRICATED  # not on it
    assert judge_field("amount", None, 49.99, PROMO) is Outcome.FABRICATED  # bait taken
    assert judge_field("amount", None, None, PROMO) is Outcome.CORRECT


def test_inferred_fields_are_never_fabricated():
    assert judge_field("currency", None, "AUD", PROMO) is Outcome.WRONG
    assert judge_field("period", "quarterly", "monthly", BILL) is Outcome.WRONG
    assert judge_field("doc_type", DocType.DISPOSABLE, DocType.INVOICE, PROMO) is Outcome.WRONG


def test_scan_wrong_value_counts_as_fabricated():
    # No text to find the value in: the strict rule for scans.
    assert judge_field("amount", 18.99, 19.99, "") is Outcome.FABRICATED
    assert judge_field("amount", 18.99, 18.99, "") is Outcome.CORRECT


def test_key_fields_depend_on_type():
    assert key_fields(bill_label()) == ("vendor", "amount", "due_date")
    assert key_fields(bill_label(doc_type="receipt")) == ("vendor", "amount", "issue_date")
    assert key_fields(bill_label(doc_type="statement", amount=None, due_date=None)) == ()
    assert key_fields(bill_label(doc_type="statement")) == ("vendor", "amount", "due_date")
    assert key_fields(bill_label(doc_type="contract")) == ()


# --- whole documents ---------------------------------------------------------------------


def test_fully_correct_ignores_non_key_fields():
    r = score(
        [bill_label()], [answer("bill.txt", **GOOD, period="monthly")], {"bill.txt": BILL}, 0.8
    )
    assert r.fully_correct == 1
    assert r.docs[0].outcomes["period"] is Outcome.WRONG  # still counted per field
    assert r.red_line_ok and not r.docs[0].reviewed


def test_wrong_type_makes_the_document_wrong():
    lab = bill_label(
        file="p.txt",
        doc_type="disposable",
        vendor="Harvey Norman",
        amount=None,
        currency=None,
        due_date=None,
    )
    pred = answer("p.txt", vendor="Harvey Norman")  # called it an invoice, no values made up
    r = score([lab], [pred], {"p.txt": PROMO}, 0.8)
    assert r.fully_correct == 0 and r.fabrications == 0


def test_confident_bait_crosses_the_red_line():
    lab = bill_label(
        file="p.txt",
        doc_type="disposable",
        vendor="Harvey Norman",
        amount=None,
        currency=None,
        due_date=None,
    )
    pred = answer("p.txt", confidence=0.95, vendor="Harvey Norman", amount=49.99)
    r = score([lab], [pred], {"p.txt": PROMO}, 0.8)
    assert r.fabrications == 1
    assert not r.red_line_ok  # 49.99 is on the page, so the source check can't stop it


def test_unsure_bait_is_caught_by_review():
    lab = bill_label(
        file="p.txt",
        doc_type="disposable",
        vendor="Harvey Norman",
        amount=None,
        currency=None,
        due_date=None,
    )
    pred = answer("p.txt", confidence=0.6, vendor="Harvey Norman", amount=49.99)
    r = score([lab], [pred], {"p.txt": PROMO}, 0.8)
    assert r.fabrications == 1 and r.red_line_ok
    assert r.docs[0].review_reasons == ("confidence 0.60",)


def test_value_not_in_document_goes_to_review_whatever_the_confidence():
    pred = answer("bill.txt", confidence=0.99, **{**GOOD, "amount": 246.37})
    r = score([bill_label()], [pred], {"bill.txt": BILL}, 0.8)
    assert r.fabrications == 1 and r.red_line_ok  # the source check stopped it
    assert r.docs[0].review_reasons == ("not in document: amount",)


def test_failed_call_counts_as_wrong_but_not_fabricated():
    r = score(
        [bill_label()], [Prediction(file="bill.txt", error="timeout")], {"bill.txt": BILL}, 0.8
    )
    assert (r.fully_correct, r.failed, r.fabrications) == (0, 1, 0)
    assert r.red_line_ok
    assert r.field_counts()["amount"][Outcome.MISSED] == 1


def test_missing_prediction_counts_as_failed():
    r = score([bill_label()], [answer("other.txt", **GOOD)], {"bill.txt": BILL}, 0.8)
    assert r.failed == 1
    assert r.unlabelled_predictions == ["other.txt"]


def test_review_queue_and_calibration():
    labels = [bill_label(file=f"{i}.txt") for i in range(4)]
    texts = {f"{i}.txt": BILL for i in range(4)}
    preds = [
        answer("0.txt", 0.97, **GOOD),  # right, confident
        answer("1.txt", 0.92, **{**GOOD, "amount": 214.37}),  # wrong, confident: slips past
        answer("2.txt", 0.6, **{**GOOD, "amount": 214.37}),  # wrong, unsure: caught
        answer("3.txt", 0.7, **GOOD),  # right, unsure: a wasted review
    ]
    r = score(labels, preds, texts, 0.8)
    assert r.review() == {"reviewed": 2, "reviewed_wrong": 1, "wrong": 2, "wrong_caught": 1}
    bands = {(low, high): (n, ok) for low, high, n, ok in r.calibration()}
    assert bands[(0.95, 1.0)] == (1, 1)
    assert bands[(0.9, 0.95)] == (1, 0)
    assert bands[(0.5, 0.7)] == (1, 0)
    assert bands[(0.7, 0.8)] == (1, 1)


def test_cost_counts_cache_hits_and_latency_percentiles():
    labels = [bill_label(file=f"{i}.txt") for i in range(2)]
    texts = {f"{i}.txt": BILL for i in range(2)}
    preds = [answer("0.txt", **GOOD), answer("1.txt", **GOOD)]
    preds[1] = preds[1].model_copy(update={"cost_usd": 0.0, "latency_ms": 3000})
    r = score(labels, preds, texts, 0.8)
    assert r.cost_per_doc() == pytest.approx(0.001)
    assert r.latency() == (1200, 3000)


# --- labels ------------------------------------------------------------------------------


def test_label_rejects_unknown_keys_and_partial_dates(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps({"file": "a.pdf", "doc_type": "invoice", "due_dat": "x"}) + "\n")
    with pytest.raises(DataError, match="line 1"):
        load_labels(path)
    path.write_text(json.dumps({"file": "a.pdf", "doc_type": "invoice", "due_date": "2026-10"}))
    with pytest.raises(DataError):
        load_labels(path)


def test_label_rejects_duplicate_files(tmp_path):
    row = json.dumps({"file": "a.pdf", "doc_type": "receipt"})
    path = tmp_path / "labels.jsonl"
    path.write_text(f"{row}\n\n{row}\n")
    with pytest.raises(DataError, match="appears twice"):
        load_labels(path)


def test_example_labels_load():
    from pathlib import Path

    labels = load_labels(Path(__file__).parent.parent / "evals" / "labels.example.jsonl")
    assert labels[0].due_date == date(2026, 10, 24)


def test_read_texts_reports_every_problem(tmp_path):
    (tmp_path / "bill.txt").write_text(BILL)
    (tmp_path / "x.exe").write_bytes(b"x")
    labels = [bill_label(), bill_label(file="gone.pdf"), bill_label(file="x.exe")]
    with pytest.raises(DataError) as e:
        read_texts(labels, tmp_path)
    assert "gone.pdf: file not found" in str(e.value)
    assert "x.exe: unsupported file type" in str(e.value)
    assert read_texts([bill_label()], tmp_path)["bill.txt"].startswith("Origin Energy")


# --- report and history ------------------------------------------------------------------


def test_render_lists_slipped_documents_first():
    lab = bill_label(
        file="p.txt",
        doc_type="disposable",
        vendor="Harvey Norman",
        amount=None,
        currency=None,
        due_date=None,
    )
    r = score(
        [bill_label(), lab],
        [answer("bill.txt", **{**GOOD, "amount": 214.37}), answer("p.txt", amount=49.99)],
        {"bill.txt": BILL, "p.txt": PROMO},
        0.8,
    )
    text = render(r)
    assert text.startswith("Red line: FAILED")
    assert text.index("p.txt  [SLIPPED") < text.index("bill.txt  [")
    assert "amount: fabricated  label —  model 49.99" in text


def test_verdict_order():
    base = {"red_line_ok": True, "fabrications": 1, "fully_correct": 20}
    assert verdict({**base, "red_line_ok": False}, base).startswith("REJECT: a fabricated")
    assert verdict({**base, "fabrications": 2}, base).startswith("REJECT: more fabrications")
    assert verdict({**base, "fully_correct": 18}, base).startswith("REJECT: fewer")
    assert verdict({**base, "fully_correct": 19}, base).startswith("OK")  # 1 document: flat
    assert verdict({**base, "failed": 1}, {**base, "failed": 0}).startswith("REJECT: more failed")
    assert verdict(base, None) == "first run on these labels"


def test_history_keeps_totals_only_and_compares_same_labels(tmp_path):
    r = score([bill_label()], [answer("bill.txt", **GOOD)], {"bill.txt": BILL}, 0.8)
    jsonl, md = tmp_path / "results.jsonl", tmp_path / "results.md"
    save_run(r, jsonl, md, today=date(2026, 11, 22))
    history = load_history(jsonl)
    assert "bill.txt" not in jsonl.read_text() and "bill.txt" not in md.read_text()
    assert previous_run(history, r.fingerprint)["fully_correct"] == 1
    assert previous_run(history, "other-labels") is None
    assert "| 2026-11-22 |" in md.read_text()
