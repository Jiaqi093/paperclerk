"""Judge one field of one document: correct, missed, fabricated or wrong (labeling guide §2–§4)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from paperclerk.evals.data import Label
from paperclerk.extract.schemas import DocType
from paperclerk.extract.source_check import (
    LITERAL_FIELDS,
    amount_in_text,
    date_in_text,
    normalize_vendor,
    vendor_in_text,
)

SCORED_FIELDS = (
    "doc_type",
    "vendor",
    "amount",
    "currency",
    "issue_date",
    "due_date",
    "is_recurring",
    "period",
)


class Outcome(StrEnum):
    CORRECT = "correct"
    MISSED = "missed"  # on the document, the model gave nothing: a person fills it in
    FABRICATED = "fabricated"  # made up: the label is empty, or the value isn't in the document
    WRONG = "wrong"  # a value that is on the document, but the wrong one


def values_match(field: str, expected: Any, got: Any) -> bool:
    if expected is None or got is None:
        return expected is None and got is None
    if field == "vendor":
        return normalize_vendor(expected) == normalize_vendor(got)
    if field == "amount":
        return abs(float(expected) - float(got)) <= 0.01 + 1e-9
    if field == "currency":
        return str(expected).upper() == str(got).upper()
    return expected == got  # type, dates, recurring, period: exact


def _in_text(field: str, value: Any, text: str) -> bool:
    if field == "vendor":
        return vendor_in_text(value, text)
    if field == "amount":
        return amount_in_text(value, text)
    return date_in_text(value, text)


def judge_field(field: str, expected: Any, got: Any, text: str) -> Outcome:
    if values_match(field, expected, got):
        return Outcome.CORRECT
    if got is None:
        return Outcome.MISSED
    if field in LITERAL_FIELDS and (expected is None or not _in_text(field, got, text)):
        return Outcome.FABRICATED
    return Outcome.WRONG


_KEY_FIELDS: dict[DocType, tuple[str, ...]] = {
    DocType.INVOICE: ("vendor", "amount", "due_date"),
    DocType.RECEIPT: ("vendor", "amount", "issue_date"),
    DocType.SUBSCRIPTION: ("vendor", "amount", "period", "due_date"),
}


def key_fields(label: Label) -> tuple[str, ...]:
    """Fields that decide whether a document is fully correct, besides its type (guide §3)."""
    if label.doc_type is DocType.STATEMENT and (label.amount is not None or label.due_date):
        return ("vendor", "amount", "due_date")  # a credit card statement: still has to be paid
    return _KEY_FIELDS.get(label.doc_type, ())
