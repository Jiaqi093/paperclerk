from datetime import date

import pytest

from paperclerk.extract.schemas import DocType, Extraction
from paperclerk.extract.source_check import (
    amount_in_text,
    date_in_text,
    normalize_vendor,
    unsupported_fields,
    vendor_in_text,
)


@pytest.mark.parametrize(
    "name",
    ["Origin Energy Pty Ltd", "ORIGIN ENERGY", "The Origin Energy Pty. Ltd.", "origin  energy"],
)
def test_legal_suffixes_and_case_are_ignored(name):
    assert normalize_vendor(name) == "origin energy"


def test_only_trailing_suffix_words_are_dropped():
    assert normalize_vendor("Co-op Insurance") == "co op insurance"
    assert normalize_vendor("Johnson & Johnson Inc") == "johnson and johnson"
    assert normalize_vendor("Ltd") == "ltd"  # never reduced to nothing


def test_vendor_found_as_whole_words():
    text = "ORIGIN ENERGY RETAIL PTY LTD\nABN 22 078 868 425"
    assert vendor_in_text("Origin Energy", text)
    assert vendor_in_text("Origin Energy Retail Pty Ltd", text)
    assert not vendor_in_text("AGL", text)
    assert not vendor_in_text("Origin Gas", text)
    assert not vendor_in_text("Rig", "Origin Energy")  # inside a word doesn't count


@pytest.mark.parametrize(
    "text",
    ["Total due $1,200.00", "AUD 1200", "Betrag 1.200,00 EUR", "pay 1200.00 by", "A$ 1,200"],
)
def test_amount_found_in_printed_forms(text):
    assert amount_in_text(1200.0, text)


@pytest.mark.parametrize(
    "text",
    ["Total $1,200.50", "ref 21200", "12000", "Total $120.00", "AMOUNT 1200", "1200 items"],
)
def test_amount_not_found_in_other_numbers(text):
    assert not amount_in_text(1200.0, text)


def test_amount_cents():
    assert amount_in_text(214.37, "Amount due: $214.37 (incl. GST)")
    assert not amount_in_text(241.37, "Amount due: $214.37 (incl. GST)")


@pytest.mark.parametrize(
    "text",
    [
        "Due 2026-11-05",
        "Due date: 05/11/2026",
        "due 5.11.26",
        "Pay by 5 Nov 2026",
        "Pay by 5th November, 2026",
        "November 5, 2026",
        "05-Nov-2026",
        "will be debited on 5 Nov",  # no year printed
    ],
)
def test_date_found_in_printed_forms(text):
    assert date_in_text(date(2026, 11, 5), text)


@pytest.mark.parametrize(
    "text", ["Due 15 Nov 2026", "Due 5 Nov 2027", "Due 2026-11-15", "Due 5 Dec 2026", "Nov 2026"]
)
def test_date_not_found_in_other_dates(text):
    assert not date_in_text(date(2026, 11, 5), text)


def test_unsupported_fields_lists_values_not_in_the_text():
    text = "Origin Energy\nTotal due $214.37\nDue date 24 Oct 2026"
    ok = Extraction(
        doc_type=DocType.INVOICE,
        vendor="Origin Energy Pty Ltd",
        amount=214.37,
        due_date=date(2026, 10, 24),
        currency="AUD",  # inferred fields are never checked
        confidence=0.9,
    )
    assert unsupported_fields(ok, text) == []
    made_up = ok.model_copy(update={"amount": 241.37, "issue_date": date(2026, 10, 3)})
    assert unsupported_fields(made_up, text) == ["amount", "issue_date"]


def test_scanned_document_has_nothing_to_check_against():
    ex = Extraction(doc_type=DocType.RECEIPT, vendor="Netflix", amount=18.99, confidence=0.99)
    assert unsupported_fields(ex, "") == ["vendor", "amount"]


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (24.10, "Issued 24.10.2026"),  # part of a date
        (2026, "Invoice 2026"),  # a year
        (345, "ABN 12 345 678 901"),
        (1300, "Call 1300 123 456"),
        (3000, "Melbourne VIC 3000"),
        (0, "0 items"),
        (214.375, "Total 214.375"),  # more than 2 decimals is never an amount
        (200, "Total 1 200,00 €"),  # space as thousands separator: the amount is 1200
        (214.37, "Usage 214.37 kWh"),
        (10.5, "rate 10.50%"),
        (14.3, "delivered at 14.30 pm"),
    ],
)
def test_numbers_that_are_not_money_dont_count(amount, text):
    assert not amount_in_text(amount, text)


def test_whole_amount_needs_a_currency_next_to_it():
    assert amount_in_text(214, "Total $214")
    assert amount_in_text(214, "Total 214 AUD")
    assert not amount_in_text(214, "Total 214")


@pytest.mark.parametrize(
    "text",
    ["Due 25 November 2026", "Due 25 Nov. 2026", "November 25th, 2026", "25-Nov-26"],
)
def test_date_with_another_year_is_not_found(text):
    assert not date_in_text(date(2025, 11, 25), text)
    assert date_in_text(date(2026, 11, 25), text)


def test_day_month_swap_is_not_found():
    assert not date_in_text(date(2026, 5, 11), "Issued 05/11/2026")  # 5 Nov, not 11 May
    assert date_in_text(date(2026, 11, 5), "Issued 05/11/2026")
    assert date_in_text(date(2026, 11, 25), "Issued 11/25/2026")  # US order, unambiguous


@pytest.mark.parametrize("vendor", ["The", "Pty Ltd", "And Co", "Ltd", "Bank", "Energy Pty Ltd"])
def test_generic_words_are_never_a_vendor(vendor):
    assert not vendor_in_text(vendor, "The Origin Energy Pty Ltd and Co, your bank account")
