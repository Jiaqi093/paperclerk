"""F2 step 3: is the answer backed by the document? Two checks, run on the document's own text
(never on the file name, never on the markup sent to the model):

1. Source check: each company, amount and date the model gave is written in the document.
2. Evidence check: for each of them the model quoted the document, the quote really is in the
   document, and the quote shows that value.

Neither check changes the answer. Each problem is a reason to send the document to review.

A value read from a scanned page has no text to be found in. Such problems are marked
`scanned`: they can't be settled by asking another model, only by a person (or, later, by
text recognised on the device).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from paperclerk.extract.schemas import Extraction
from paperclerk.extract.source_check import (
    LITERAL_FIELDS,
    amount_in_text,
    date_in_text,
    normalize_vendor,
    vendor_in_text,
)

_LABELS = {
    "vendor": "company",
    "amount": "amount",
    "issue_date": "issue date",
    "due_date": "due date",
}


@dataclass(frozen=True)
class Problem:
    field: str  # one of LITERAL_FIELDS
    what: str  # "not in the document", "no quote", "quote not in the document", ...
    scanned: bool = False  # the document has scanned pages, so this can't be checked in text

    def __str__(self) -> str:
        label = _LABELS.get(self.field, self.field)
        tail = " (may be on a scanned page, which can't be checked)" if self.scanned else ""
        return f"{label}: {self.what}{tail}"


def check(extraction: Extraction, text: str, *, has_scanned_pages: bool = False) -> list[Problem]:
    """Problems with the company, amount and dates in `extraction`, against the document's
    text. Empty: every value given is written in the document and quoted from it."""
    problems: list[Problem] = []
    flat_text = _flat(text)
    evidence = extraction.evidence
    for field in LITERAL_FIELDS:
        value = getattr(extraction, field)
        if value is None:
            continue
        if not _shows(field, value, text):
            problems.append(Problem(field, "not in the document", has_scanned_pages))
            continue  # the quote can't fix that; one reason per field is enough
        quote = getattr(evidence, field, None) if evidence is not None else None
        if not quote or not quote.strip():
            problems.append(Problem(field, "no quote from the document"))
        elif not _quote_in(quote, flat_text):
            problems.append(Problem(field, "quote not in the document", has_scanned_pages))
        elif not _shows(field, value, quote, document=text):
            problems.append(Problem(field, "quote doesn't show this value"))
    return problems


def can_retry(problems: list[Problem]) -> bool:
    """Worth asking a stronger model: some problem is in text a model can be checked against."""
    return any(not p.scanned for p in problems)


# How bad a problem is, for choosing between two answers: a value that isn't in the document
# may be made up; a missing or loose quote is a sloppier answer, not a wrong value.
_WEIGHT = {"not in the document": 3}
DROPPED_WEIGHT = 3  # a value one answer gave and the other left out


def badness(problems: list[Problem]) -> int:
    return sum(_WEIGHT.get(p.what, 1) for p in problems)


def _quote_in(quote: str, flat_text: str) -> bool:
    """The quote is in the document as whole words and numbers: "5 Aug 2026" isn't found in
    "15 Aug 2026", nor "36.46" in "236.46"."""
    flat_quote = _flat(quote)
    if not flat_quote:
        return False
    # Boundaries only where the quote starts or ends with a letter or digit, so "$264.37" is
    # found in "AUD$264.37". A number can't be cut out of a longer one at a separator either:
    # "200.00" isn't in "1,200.00", nor "$1,200" in "$1,200.00".
    before = after = ""
    if flat_quote[0].isalnum():
        before = r"(?<![^\W_])" + (r"(?<!\d[.,])" if flat_quote[0].isdigit() else "")
    if flat_quote[-1].isalnum():
        after = r"(?![^\W_])" + (r"(?![.,]\d)" if flat_quote[-1].isdigit() else "")
    return re.search(before + re.escape(flat_quote) + after, flat_text) is not None


def _shows(field: str, value, text: str, *, document: str | None = None) -> bool:
    if field == "vendor":
        return vendor_in_text(value, text)
    if field == "amount":
        return amount_in_text(value, text)
    return date_in_text(value, text, document=document)


def same_value(field: str, a, b) -> bool:
    """Two answers agree on a field. Company names agree without legal suffixes, and when one
    is the other written shorter ("Origin" and "Origin Energy"): the same brand."""
    if field == "vendor":
        x, y = normalize_vendor(a), normalize_vendor(b)
        return f" {x} " in f" {y} " or f" {y} " in f" {x} "
    if field == "amount":
        return abs(a - b) < 0.005
    return a == b


_TYPOGRAPHY = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def _flat(text: str) -> str:
    """For comparing a quote with the document: PDF text breaks lines and spaces oddly and a
    model tidies them when it copies, so case, spacing, line breaks, curly quotes and dashes
    don't count, nor a space between a currency sign and its number ("$ 264.37"). The words
    and figures must be the same."""
    text = unicodedata.normalize("NFKC", text).casefold().translate(_TYPOGRAPHY)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")  # soft hyphens etc.
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"([$€£¥])\s+(?=\d)", r"\1", text)
