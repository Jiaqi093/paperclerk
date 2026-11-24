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
        "Issued 17 Oct 2026. Will be debited on 5 Nov",  # year printed elsewhere
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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("OJC MARKETING SDN BHD", "ojc marketing"),
        ("99 SPEED MART S/B", "99 speed mart"),
        ("AEON CO. (M) BHD", "aeon"),
        ("MR. D.I.Y. (M) SDN BHD", "mr d i y"),
        ("Plan B", "plan b"),  # a lone B is not S/B
    ],
)
def test_malaysian_legal_forms_are_ignored(name, expected):
    assert normalize_vendor(name) == expected


def test_abbreviation_after_an_amount_is_not_a_unit():
    assert amount_in_text(164.0, "Total Invoice Value 164.00 G.S.T. 14.90")
    assert not amount_in_text(214.37, "Usage 214.37 kWh.")  # a unit at the end of a sentence
    assert not amount_in_text(5.0, "net weight 5.00 g.")
    assert amount_in_text(164.0, "Total 164.00 G.S.T 14.90")


@pytest.mark.parametrize(
    ("amount", "text"),
    [(214.37, "Usage 214.37 kWh.Total due $50.00"), (5.0, "5.00 g.Price"), (12.5, "12.50 km.Fare")],
)
def test_unit_before_a_full_stop_without_space_is_still_a_unit(amount, text):
    # PDF text often drops the space after a full stop
    assert not amount_in_text(amount, text)


@pytest.mark.parametrize(
    ("vendor", "text"),
    [
        ("ABC S/B (Kepong)", "ABC S/B (Kepong) tax invoice"),
        ("AEON (M) Credit", "AEON (M) CREDIT"),
        ("Nestle Products", "NESTLE PRODUCTS (MALAYSIA) SDN BHD"),
        ("XYZ", "XYZ S/BHD"),
    ],
)
def test_malaysian_forms_match_the_text_as_printed(vendor, text):
    assert vendor_in_text(vendor, text)


def test_slash_b_inside_ordinary_words_is_left_alone():
    assert normalize_vendor("Jack's/B&Q") != normalize_vendor("Jack")


def test_review_edge_cases():
    assert not vendor_in_text("Length Width", "Length (m) Width (m)")  # a unit, not Malaysia
    assert vendor_in_text("AEON", "AEON CO. (M) BHD")
    assert normalize_vendor("Jack’s/B&Q") == normalize_vendor("Jack's/B&Q")
    assert not amount_in_text(80.0, "speed 80.00 m.p.h.")
    assert not amount_in_text(90.0, "paper 90.00 g.s.m.")


@pytest.mark.parametrize(
    "text",
    [
        "will be debited on 5 Nov",  # no year anywhere: the year would be a guess
        "Billing period 5 Nov to 4 Dec 2025, issued 10 Dec 2026",  # two years: either one
        "Issued 17 Oct 2027. Will be debited on 5 Nov",  # another year
    ],
)
def test_yearless_date_needs_the_year_printed_and_only_that_one(text):
    assert not date_in_text(date(2026, 11, 5), text)


def test_may_without_a_year_is_not_a_date():
    assert not date_in_text(date(2027, 5, 5), "Late fees of 5 may apply. Issued 1 Apr 2027")
    assert date_in_text(date(2027, 5, 5), "Due 5 May 2027")


def test_postcode_is_not_a_year():
    text = "Sydney NSW 2000\nIssued 17 Oct 2026. Will be debited on 5 Nov"
    assert date_in_text(date(2026, 11, 5), text)
