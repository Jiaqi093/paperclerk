"""Source check: is each extracted company, amount and date actually written in the document?

A value that can't be found may be made up, so the document goes to human review instead of
straight into the library. F2 runs this on every extraction; F6 runs the same check to decide
whether a fabricated value would have slipped through. Values are matched in the forms
documents use ("$1,200.00" for 1200, "25 Nov 2026" for 2026-11-25), never by fuzzy guessing:
a miss only costs a review.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from paperclerk.extract.schemas import Extraction

# Fields that are always written on a document, so they must be found in its text.
LITERAL_FIELDS = ("vendor", "amount", "issue_date", "due_date")

# Legal-form words dropped from the end of a company name: "Origin Energy Pty Ltd" = "Origin
# Energy". Only trailing words, so "Co-op Insurance" keeps its "co".
_LEGAL_SUFFIXES = frozenset(
    "pty ltd limited inc incorporated llc plc corp corporation co company gmbh ag sa bv nv pte"
    " lp llp".split()
)


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold().replace("&", " and ")
    return re.findall(r"[^\W_]+", text)


def normalize_vendor(name: str) -> str:
    """Company name reduced to the words that identify it."""
    words = _words(name)
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


# Words that say nothing about which company it is: a name made only of these never counts
# as found ("Pty Ltd", "The", "And Co").
_GENERIC = _LEGAL_SUFFIXES | {
    "the", "and", "of", "bank", "energy", "insurance", "services", "group", "account", "online",
}  # fmt: skip


def vendor_in_text(vendor: str, text: str) -> bool:
    name = normalize_vendor(vendor)
    if not any(len(w) >= 2 and w not in _GENERIC for w in name.split()):
        return False
    return f" {name} " in f" {' '.join(_words(text))} "


# A number as printed: 1,200.00 / 1200.5 / 1.200,00 (European) / 214. Not part of a longer
# run such as a date ("24.10.2026"), a version ("1.2.3") or a space-grouped number ("1 200,00",
# where "200,00" alone would look like an amount).
_NUMBER = re.compile(
    r"(?<![\d.,])(?<!\d[ \u00a0\u202f])"
    r"(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![.,]?\d)"
)
_CURRENCY = r"(?:[$€£¥]|\b(?:AUD|USD|EUR|GBP|NZD|CAD|JPY|CNY|RMB|SGD|HKD|CHF|INR)\b)"
_CURRENCY_BEFORE = re.compile(_CURRENCY + r"\s*-?\s*$", re.IGNORECASE)
_CURRENCY_AFTER = re.compile(r"^\s*" + _CURRENCY, re.IGNORECASE)
# A unit or time after the number: a usage figure or a clock time, not money.
_UNIT_AFTER = re.compile(
    r"^\s*(?:%|:\d|(?:kwh|kw|mwh|mj|gj|gb|mb|tb|kg|g|l|ml|km|m|c|cents?|units?|days?|hours?|hrs?"
    r"|am|pm)\b)",
    re.IGNORECASE,
)


def _parse_number(token: str) -> Decimal | None:
    # The last separator followed by 1–2 digits is the decimal point; the others group thousands.
    m = re.match(r"^(.*?)(?:([.,])(\d{1,2}))?$", token)
    if m is None:
        return None
    whole, _, frac = m.groups()
    whole = re.sub(r"[.,]", "", whole)
    try:
        return Decimal(f"{whole}.{frac}" if frac else whole)
    except InvalidOperation:
        return None


def _looks_like_money(text: str, m: re.Match[str]) -> bool:
    """Next to a currency ("$214", "214 AUD"), or printed with cents ("214.37", "1.200,00") and
    not followed by a unit ("214.37 kWh", "10.50%"). A bare integer is too often something
    else: a year, a postcode, part of a phone number."""
    if _CURRENCY_BEFORE.search(text[max(0, m.start() - 6) : m.start()]) or _CURRENCY_AFTER.match(
        text[m.end() : m.end() + 6]
    ):
        return True
    return bool(re.search(r"[.,]\d{2}$", m.group())) and not _UNIT_AFTER.match(
        text[m.end() : m.end() + 8]
    )


def amount_in_text(amount: float, text: str) -> bool:
    target = Decimal(str(amount))
    if target != target.quantize(Decimal("0.01")):
        return False  # more than 2 decimals: no document prints that as an amount
    text = unicodedata.normalize("NFKC", text)
    for m in _NUMBER.finditer(text):
        value = _parse_number(m.group())
        if value is not None and value == target and _looks_like_money(text, m):
            return True
    return False


_MONTHS = [
    ("january", "jan"),
    ("february", "feb"),
    ("march", "mar"),
    ("april", "apr"),
    ("may", "may"),
    ("june", "jun"),
    ("july", "jul"),
    ("august", "aug"),
    ("september", "sep", "sept"),
    ("october", "oct"),
    ("november", "nov"),
    ("december", "dec"),
]


def _date_pattern(d: date) -> re.Pattern[str]:
    # The lookaheads stop a shorter match inside a longer word or number: "nov" inside
    # "November 2026", "25" inside "25th, 2026". Without them the year-less forms below would
    # accept a date printed with a different year.
    day = rf"0?{d.day}(?:st|nd|rd|th)?(?![a-z])"
    month_num = rf"0?{d.month}"
    year = rf"(?:{d.year}|{d.year % 100:02d})"
    month_name = "(?:" + "|".join(_MONTHS[d.month - 1]) + r")(?![a-z])\.?"
    sep = r"\s*[/\-.]\s*"
    no_year_after = r"(?![\s\-,.]*\d)"
    forms = [
        rf"{d.year}{sep}{month_num}{sep}0?{d.day}",  # 2026-11-25
        rf"0?{d.day}{sep}{month_num}{sep}{year}",  # 25/11/2026, 25.11.26
        rf"{day}(?:\s+of)?[\s\-]*{month_name}[\s\-,]*{year}",  # 25 Nov 2026, 25-Nov-26
        rf"{month_name}\s*{day}[\s,]*{year}",  # November 25, 2026
        # No year printed ("debited on 25 Nov"): matches unless a year follows.
        rf"{day}(?:\s+of)?[\s\-]*{month_name}{no_year_after}",
        rf"{month_name}\s*{day}{no_year_after}",
    ]
    if d.day > 12:
        # US order only when it can't be a day/month swap: 11/25/2026 is unambiguous, while
        # 05/11/2026 would let a misread 11 May pass for 5 Nov.
        forms.append(rf"{month_num}{sep}0?{d.day}{sep}{year}")
    return re.compile(r"(?<![\d])(?:" + "|".join(forms) + r")(?![\d])", re.IGNORECASE)


def date_in_text(d: date, text: str) -> bool:
    return _date_pattern(d).search(unicodedata.normalize("NFKC", text)) is not None


def unsupported_fields(extraction: Extraction, text: str) -> list[str]:
    """The company, amount and date fields whose value isn't written in `text`.

    A scanned document has little or no text, so its values are usually reported here: it
    goes to review, which is the intended outcome until scans can be checked another way.
    """
    missing = []
    if extraction.vendor is not None and not vendor_in_text(extraction.vendor, text):
        missing.append("vendor")
    if extraction.amount is not None and not amount_in_text(extraction.amount, text):
        missing.append("amount")
    for field in ("issue_date", "due_date"):
        value = getattr(extraction, field)
        if value is not None and not date_in_text(value, text):
            missing.append(field)
    return missing
