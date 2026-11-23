"""Labeling helpers: which fields to ask for each type, and strict parsing of typed answers.

The interactive loop is evals/label.py. It never shows a model answer (guide: label first).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from paperclerk.extract.schemas import DocType, Period
from paperclerk.ingest.parsers import SUPPORTED_SUFFIXES

_MONEY = ("amount", "currency")
# Fields asked after the type. Anything not listed is always empty for that type (guide §2),
# so it isn't asked: a receipt has no due date, a disposable document no amount.
FIELDS_BY_TYPE: dict[DocType, tuple[str, ...]] = {
    DocType.INVOICE: ("vendor", *_MONEY, "issue_date", "due_date", "is_recurring", "period"),
    DocType.RECEIPT: ("vendor", *_MONEY, "issue_date", "is_recurring", "period"),
    DocType.SUBSCRIPTION: ("vendor", *_MONEY, "issue_date", "due_date", "is_recurring", "period"),
    DocType.STATEMENT: ("vendor", "issue_date", *_MONEY, "due_date"),
    DocType.IMPORTANT: ("vendor", "issue_date", *_MONEY),
    DocType.CONTRACT: ("vendor", "issue_date", *_MONEY),
    DocType.WORK: ("vendor", "issue_date"),
    DocType.DISPOSABLE: ("vendor", "issue_date"),
    DocType.OTHER: ("vendor", "issue_date", *_MONEY),
}

HINTS: dict[str, str] = {
    "vendor": "company as people know it, e.g. Origin Energy",
    "amount": "invoice: total to pay now; receipt: paid; subscription: per period;"
    " statement: credit card closing balance only",
    "currency": "three letters, e.g. AUD",
    "issue_date": "YYYY-MM-DD; receipt: payment date",
    "due_date": "YYYY-MM-DD; pay-by / debit date, next renewal; credit card: payment due",
    "is_recurring": "y / n",
    "period": "weekly, monthly, quarterly, yearly",
}


class InvalidAnswer(ValueError):
    pass


def parse_doc_type(text: str) -> DocType:
    """By number (1–9, in the guide's tie-break order) or by name."""
    text = text.strip().lower()
    types = list(DocType)
    if text.isdigit() and 1 <= int(text) <= len(types):
        return types[int(text) - 1]
    try:
        return DocType(text)
    except ValueError:
        raise InvalidAnswer(f"type must be 1–{len(types)} or a name") from None


def parse_answer(field: str, text: str) -> object:
    """A typed answer → the label value. Empty input means the document doesn't show it."""
    text = text.strip()
    if not text:
        return None
    if field == "vendor":
        return text
    if field == "amount":
        cleaned = text.replace(",", "").lstrip("$€£¥").strip()
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            raise InvalidAnswer("amount like 1200.50") from None
        if value < 0 or value != value.quantize(Decimal("0.01")):
            raise InvalidAnswer("amount must be positive with at most 2 decimals")
        return float(value)
    if field == "currency":
        if len(text) != 3 or not text.isalpha():
            raise InvalidAnswer("currency is three letters, e.g. AUD")
        return text.upper()
    if field in ("issue_date", "due_date"):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise InvalidAnswer("date as YYYY-MM-DD (a full day; else leave empty)") from None
    if field == "is_recurring":
        if text.lower() in ("y", "yes"):
            return True
        if text.lower() in ("n", "no"):
            return False
        raise InvalidAnswer("y or n")
    if field == "period":
        try:
            return Period(text.lower()).value
        except ValueError:
            raise InvalidAnswer("weekly, monthly, quarterly or yearly") from None
    raise KeyError(field)


def unlabelled_files(root: Path, labelled: Iterable[str]) -> list[str]:
    """Documents under `root` without a label yet, as paths relative to it."""
    done = set(labelled)
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if (
            path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
            and rel not in done
        ):
            found.append(rel)
    return found
