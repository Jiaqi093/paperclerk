import json
import subprocess
import sys
from pathlib import Path

import pytest

from paperclerk.evals.labeling import (
    FIELDS_BY_TYPE,
    InvalidAnswer,
    parse_answer,
    parse_doc_type,
    unlabelled_files,
)
from paperclerk.extract.schemas import DocType

ROOT = Path(__file__).parent.parent


def test_doc_type_by_number_or_name():
    assert parse_doc_type("1") is DocType.INVOICE
    assert parse_doc_type(" Receipt ") is DocType.RECEIPT
    with pytest.raises(InvalidAnswer):
        parse_doc_type("10")


def test_every_type_has_its_fields_and_receipts_never_get_a_due_date():
    assert set(FIELDS_BY_TYPE) == set(DocType)
    assert "due_date" not in FIELDS_BY_TYPE[DocType.RECEIPT]
    assert "amount" not in FIELDS_BY_TYPE[DocType.DISPOSABLE]
    assert all("vendor" in fields for fields in FIELDS_BY_TYPE.values())


@pytest.mark.parametrize(
    ("field", "text", "value"),
    [
        ("amount", "$1,200.50", 1200.5),
        ("amount", "", None),
        ("currency", "aud", "AUD"),
        ("due_date", "2026-11-25", "2026-11-25"),
        ("is_recurring", "Y", True),
        ("period", "Monthly", "monthly"),
        ("vendor", "  Origin Energy ", "Origin Energy"),
    ],
)
def test_answers_parse(field, text, value):
    assert parse_answer(field, text) == value


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("amount", "12.345"),
        ("amount", "-5"),
        ("amount", "abc"),
        ("currency", "A$"),
        ("due_date", "25/11/2026"),
        ("due_date", "2026-11"),
        ("is_recurring", "maybe"),
        ("period", "fortnightly"),
    ],
)
def test_bad_answers_are_refused(field, text):
    with pytest.raises(InvalidAnswer):
        parse_answer(field, text)


def test_unlabelled_files_skips_done_hidden_and_unsupported(tmp_path):
    for name in ("a.pdf", "b.txt", "sub/c.png", ".hidden/d.pdf", "labels.jsonl", "e.exe"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert unlabelled_files(tmp_path, ["a.pdf"]) == ["b.txt", "sub/c.png"]


def test_run_eval_end_to_end(tmp_path):
    (tmp_path / "bill.txt").write_text("Origin Energy\nTotal due $264.37\nDue 24 Oct 2026")
    label = {
        "file": "bill.txt",
        "doc_type": "invoice",
        "vendor": "Origin Energy",
        "amount": 264.37,
        "due_date": "2026-10-24",
    }
    (tmp_path / "labels.jsonl").write_text(json.dumps(label) + "\n")
    out = {
        "doc_type": "invoice",
        "vendor": "Origin Energy",
        "amount": 264.37,
        "due_date": "2026-10-24",
        "confidence": 0.9,
    }
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(json.dumps({"file": "bill.txt", "output": out, "cost_usd": 0.003}) + "\n")
    cmd = [
        sys.executable,
        str(ROOT / "evals" / "run_eval.py"),
        "--labels",
        str(tmp_path / "labels.jsonl"),
        "--predictions",
        str(preds),
        "--threshold",
        "0.8",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "Red line: PASSED" in result.stdout
    assert "Fully correct:  1/1 (100%)" in result.stdout

    out["amount"] = 49.99  # made up, confident, not on the page → sent to review, red line holds
    preds.write_text(json.dumps({"file": "bill.txt", "output": out}) + "\n")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "Fabrications:   1" in result.stdout


def test_run_eval_stops_when_predictions_dont_match_labels(tmp_path):
    (tmp_path / "bill.txt").write_text("Total due $264.37")
    (tmp_path / "labels.jsonl").write_text(json.dumps({"file": "bill.txt", "doc_type": "invoice"}))
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(json.dumps({"file": "/abs/bill.txt", "error": "x"}) + "\n")
    cmd = [
        sys.executable,
        str(ROOT / "evals" / "run_eval.py"),
        "--labels",
        str(tmp_path / "labels.jsonl"),
        "--predictions",
        str(preds),
        "--threshold",
        "0.8",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "have no prediction" in result.stderr
