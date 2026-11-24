"""F2 step 3: source and evidence check, scans to the vision model, and the second read."""

from __future__ import annotations

from datetime import date

from paperclerk.extract.classifier import extract
from paperclerk.extract.schemas import DocType, Evidence, Extraction, ModelAnswer
from paperclerk.extract.verify import Problem, can_retry, check
from paperclerk.ingest.parsers import Page
from paperclerk.ingest.parsers.base import ParsedDocument
from paperclerk.llm.base import LLMError, LLMResult, Usage

TEXT = "ORIGIN ENERGY\nThis period $214.37\nAmount due\n$236.46\nNew charges due 05 Aug 2026"
QUOTES = {"vendor": "Origin Energy", "amount": "Amount due $236.46",
          "issue_date": None, "due_date": "New charges due 05 Aug 2026"}  # fmt: skip


def _ex(evidence=QUOTES, **kw) -> Extraction:
    values = dict(vendor="Origin Energy", amount=236.46, due_date=date(2026, 8, 5))
    values.update(kw)
    ev = Evidence(**evidence) if evidence is not None else None
    return Extraction(doc_type=DocType.INVOICE, confidence=0.9, evidence=ev, **values)


def test_answer_quoted_from_the_document_passes():
    # the quote spans a line break in the PDF text and differs in case: still the same words
    assert check(_ex(), TEXT) == []


def test_value_not_in_the_document():
    assert check(_ex(amount=246.37), TEXT) == [Problem("amount", "not in the document")]


def test_missing_made_up_and_mismatched_quotes():
    assert check(_ex(evidence=None), TEXT) == [
        Problem("vendor", "no quote from the document"),
        Problem("amount", "no quote from the document"),
        Problem("due_date", "no quote from the document"),
    ]
    made_up = {**QUOTES, "amount": "Total payable $236.46"}
    assert check(_ex(evidence=made_up), TEXT) == [Problem("amount", "quote not in the document")]
    wrong_line = {**QUOTES, "amount": "This period $214.37"}  # real line, other value
    assert check(_ex(evidence=wrong_line), TEXT) == [
        Problem("amount", "quote doesn't show this value")
    ]


def test_null_fields_need_no_quote():
    assert check(_ex(evidence={}, vendor=None, amount=None, due_date=None), TEXT) == []


def test_file_name_is_never_a_source():
    # the date is only in the file name, which the check never sees
    problems = check(_ex(issue_date=date(2026, 7, 15)), TEXT)
    assert problems == [Problem("issue_date", "not in the document")]


def test_scanned_problems_are_marked_and_not_worth_a_second_read():
    problems = check(_ex(), "", has_scanned_pages=True)
    assert all(p.scanned for p in problems) and not can_retry(problems)
    assert str(problems[1]) == (
        "amount: not in the document (may be on a scanned page, which can't be checked)"
    )
    assert can_retry([Problem("amount", "quote not in the document")])


# --- the flow ------------------------------------------------------------------------------

DOC = ParsedDocument("application/pdf", (Page(TEXT),))
SCAN = ParsedDocument("image/jpeg", (Page("", needs_vision=True),))


def _answer(amount: float, amount_quote: str | None = "Amount due $236.46") -> ModelAnswer:
    return ModelAnswer(
        doc_type=DocType.INVOICE, reasoning="bill", vendor="Origin Energy", amount=amount,
        currency="AUD", issue_date=None, due_date=date(2026, 8, 5), is_recurring=True,
        period="monthly", confidence=0.9,
        evidence={**QUOTES, "amount": amount_quote},
    )  # fmt: skip


def _run(doc, data, answers: dict[str, list], model="claude-sonnet-5-5", vision=None):
    log: list[str] = []

    def client_for(name):
        class Client:
            def structured(self, **kw):
                log.append(name)
                outcome = answers[name].pop(0)
                if isinstance(outcome, str):
                    raise LLMError(outcome, "boom", model=name, usage=Usage(1, 1), cost_usd=0.1)
                return LLMResult(outcome, name, Usage(10, 10), 5, 0.01)

        return Client()

    result = extract(doc, data, "b.pdf", model=model, client_for=client_for, vision_model=vision)
    return result, log


def test_clean_answer_needs_no_second_read():
    result, log = _run(DOC, b"", {"claude-sonnet-5-5": [_answer(236.46)]})
    assert log == ["claude-sonnet-5-5"] and result.problems == () and not result.fell_back


def test_failed_check_gets_one_read_by_the_stronger_model():
    result, log = _run(
        DOC, b"",
        {"claude-sonnet-5-5": [_answer(246.37)], "claude-opus-5-5": [_answer(236.46)]},
    )  # fmt: skip
    assert log == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert result.extraction.amount == 236.46 and result.problems == ()
    assert result.fell_back and result.model == "claude-opus-5-5"
    assert result.attempts[-1].second_read and result.cost_usd == 0.02


def test_second_read_kept_only_if_not_worse():
    worse = _answer(999.99, amount_quote="Total 999.99")
    worse = worse.model_copy(update={"vendor": "AGL"})
    result, _ = _run(DOC, b"", {"claude-sonnet-5-5": [_answer(246.37)], "claude-opus-5-5": [worse]})
    assert result.extraction.amount == 246.37 and not result.fell_back
    assert [str(p) for p in result.problems] == [
        "amount: not in the document",
        "company: the two reads disagree",  # the first read's company was in the document
    ]


def test_second_read_that_errors_keeps_the_first_answer():
    result, log = _run(
        DOC, b"", {"claude-sonnet-5-5": [_answer(246.37)], "claude-opus-5-5": ["api"]}
    )
    assert log == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert result.extraction.amount == 246.37 and len(result.problems) == 1


def test_strongest_model_has_no_second_read():
    result, log = _run(DOC, b"", {"claude-opus-5-5": [_answer(246.37)]}, model="claude-opus-5-5")
    assert log == ["claude-opus-5-5"] and len(result.problems) == 1


def test_scan_goes_to_the_vision_model_and_is_not_reread(tmp_path):
    from PIL import Image

    Image.new("RGB", (40, 60), "white").save(tmp_path / "s.jpg")
    data = (tmp_path / "s.jpg").read_bytes()
    result, log = _run(SCAN, data, {"claude-opus-5-5": [_answer(236.46)]}, vision="claude-opus-5-5")
    assert log == ["claude-opus-5-5"] and result.requested_model == "claude-opus-5-5"
    assert result.problems and all(p.scanned for p in result.problems)


def test_scan_without_a_vision_model_uses_the_main_one(tmp_path):
    from PIL import Image

    Image.new("RGB", (40, 60), "white").save(tmp_path / "s.jpg")
    data = (tmp_path / "s.jpg").read_bytes()
    result, log = _run(SCAN, data, {"claude-sonnet-5-5": [_answer(236.46)]})
    assert log == ["claude-sonnet-5-5"]  # scanned problems: no second read either


# --- review round 1 ---------------------------------------------------------------------


def test_quote_must_match_whole_words_and_numbers():
    text = "Invoice date 05/08/2026\nDue 15 Aug 2026\nTotal $236.46"
    ex = _ex(amount=None, vendor=None, due_date=date(2026, 8, 5),
             evidence={"vendor": None, "amount": None, "issue_date": None,
                       "due_date": "5 Aug 2026"})  # fmt: skip
    assert check(ex, text) == [Problem("due_date", "quote not in the document")]
    ex = _ex(vendor=None, due_date=None, amount=36.46, evidence={**QUOTES, "amount": "36.46"})
    assert check(ex, "Total $236.46 and fee $36.46") == []  # a real "$36.46" is there
    assert check(ex, "Total $236.46, fee 36.46 AUD") == []
    only_inside = check(ex, "Total $236.46")  # 36.46 only as part of 236.46
    assert only_inside == [Problem("amount", "not in the document")]


def test_tidied_quote_still_matches():
    text = "McDonald\u2019s Pty Ltd\nTotal due $ 264.37\nPeriod 1\u20133 Nov"
    ex = Extraction(
        doc_type=DocType.RECEIPT, vendor="McDonald's", amount=264.37, confidence=0.9,
        evidence=Evidence(vendor="McDonald's Pty Ltd", amount="Total due $264.37"),
    )  # fmt: skip
    assert check(ex, text) == []


def test_empty_second_read_does_not_win():
    empty = _answer(236.46).model_copy(update={"vendor": None, "amount": None, "due_date": None})
    result, log = _run(
        DOC, b"", {"claude-sonnet-5-5": [_answer(246.37)], "claude-opus-5-5": [empty]}
    )
    assert log == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert result.extraction.amount == 246.37 and not result.fell_back  # first read kept


def test_a_made_up_value_outweighs_a_missing_quote():
    no_quote = _answer(236.46, amount_quote=None)  # right value, no quote: 1 point
    made_up = _answer(246.37)  # value not in the document: 3 points
    result, _ = _run(DOC, b"", {"claude-sonnet-5-5": [no_quote], "claude-opus-5-5": [made_up]})
    assert result.extraction.amount == 236.46
    assert [str(p) for p in result.problems] == [
        "amount: no quote from the document",
        "amount: the two reads disagree",
    ]


def test_mixed_document_still_gets_a_second_read():
    mixed = ParsedDocument("application/pdf", (Page(TEXT), Page("", needs_vision=True)))
    log: list[str] = []
    answers = {"claude-sonnet-5-5": [_answer(246.37)], "claude-opus-5-5": [_answer(236.46)]}

    def client_for(name):
        class Client:
            def structured(self, **kw):
                log.append(name)
                return LLMResult(answers[name].pop(0), name, Usage(10, 10), 5, 0.01)

        return Client()

    from paperclerk.extract.classifier import extract_input
    from paperclerk.extract.inputs import ModelInput

    result = extract_input(
        ModelInput("doc", ()), mixed.text, has_scanned_pages=True,
        model="claude-sonnet-5-5", client_for=client_for,
    )  # fmt: skip
    assert log == ["claude-sonnet-5-5", "claude-opus-5-5"] and result.problems == ()


# --- review round 2 ---------------------------------------------------------------------


def test_second_read_cannot_swap_a_found_value_for_another_without_review():
    sloppy = _answer(236.46, amount_quote="Amount due: $236.46")  # right, quote not verbatim
    other = _answer(214.37, amount_quote="This period $214.37")  # wrong, but clean
    result, _ = _run(DOC, b"", {"claude-sonnet-5-5": [sloppy], "claude-opus-5-5": [other]})
    assert result.extraction.amount == 214.37
    assert [str(p) for p in result.problems] == ["amount: the two reads disagree"]  # reviewed


def test_second_read_leaving_values_out_is_a_reason_for_review():
    bad = _answer(246.37).model_copy(update={"vendor": "AGL"})  # 2 values not in the document
    empty = _answer(236.46).model_copy(update={"vendor": None, "amount": None, "due_date": None})
    result, _ = _run(DOC, b"", {"claude-sonnet-5-5": [bad], "claude-opus-5-5": [empty]})
    assert "due date: the second read left this out" in [str(p) for p in result.problems]


def test_yearless_quote_is_checked_against_the_documents_year():
    text = "Statement 2026\nWill be debited on 5 Nov"
    ex = _ex(vendor=None, amount=None, due_date=date(2026, 11, 5),
             evidence={"vendor": None, "amount": None, "issue_date": None,
                       "due_date": "debited on 5 Nov"})  # fmt: skip
    assert check(ex, text) == []


def test_quotes_with_punctuation_at_the_edges():
    text = "Total(incl. GST)AUD$264.37 Due 24 Oct 2026.Pay now"
    ex = Extraction(
        doc_type=DocType.INVOICE, amount=264.37, due_date=date(2026, 10, 24), confidence=0.9,
        evidence=Evidence(amount="$264.37", due_date="Due 24 Oct 2026."),
    )  # fmt: skip
    assert check(ex, text) == []


def test_number_cannot_be_cut_at_a_separator():
    text = "Total $1,200.00\nDeposit paid"
    ex = Extraction(
        doc_type=DocType.INVOICE, amount=1200.0, confidence=0.9,
        evidence=Evidence(amount="$1,200"),
    )  # fmt: skip
    assert check(ex, text) == [Problem("amount", "quote not in the document")]


def test_all_scanned_with_a_little_text_gets_no_second_read(tmp_path):
    from PIL import Image

    Image.new("RGB", (40, 60), "white").save(tmp_path / "s.jpg")
    scan = ParsedDocument("image/jpeg", (Page("Scanned by CamScanner", needs_vision=True),))
    result, log = _run(
        scan, (tmp_path / "s.jpg").read_bytes(),
        {"claude-sonnet-5-5": [_answer(236.46)]},
    )  # fmt: skip
    assert log == ["claude-sonnet-5-5"] and result.problems


def test_shorter_brand_name_is_the_same_company():
    from paperclerk.extract.verify import same_value

    assert same_value("vendor", "Origin", "Origin Energy Pty Ltd")
    assert not same_value("vendor", "Origin Energy", "AGL Energy")
