"""F2 step 4: library or review, stored with history; failures retried once. No API calls."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from paperclerk.config import LLMSettings, Settings
from paperclerk.core import PaperClerk
from paperclerk.db import connect, repo
from paperclerk.extract.classifier import ExtractionResult
from paperclerk.extract.review import review_reasons
from paperclerk.extract.schemas import DocType, Extraction, ModelAnswer
from paperclerk.extract.verify import Problem
from paperclerk.ingest.parsers import Page
from paperclerk.llm.base import LLMError, LLMResult, Usage

BILL = "Origin Energy\nAmount due $236.46\nNew charges due 05 Aug 2026"


def _answer(confidence=0.95, amount=236.46, amount_quote="Amount due $236.46") -> ModelAnswer:
    return ModelAnswer(
        doc_type=DocType.INVOICE, reasoning="a bill", vendor="Origin Energy", amount=amount,
        currency="AUD", issue_date=None, due_date=date(2026, 8, 5), is_recurring=True,
        period="monthly", confidence=confidence,
        evidence={"vendor": "Origin Energy", "amount": amount_quote, "issue_date": None,
                  "due_date": "New charges due 05 Aug 2026"},
    )  # fmt: skip


class FakeModels:
    """Each model name pops its next scripted outcome: a ModelAnswer, or an error kind."""

    def __init__(self, **script):
        self.script = {k.replace("_", "-"): list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def client_for(self, name):
        models = self

        class Client:
            def structured(self, **kw):
                models.calls.append(name)
                outcome = models.script[name].pop(0)
                if isinstance(outcome, str):
                    raise LLMError(outcome, "down", model=name)
                return LLMResult(outcome, name, Usage(1000, 200), 800, 0.004)

        return Client()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "inbox"
    root.mkdir()
    (root / "bill.txt").write_text(BILL, encoding="utf-8")
    return root


def _clerk(folder: Path, models: FakeModels, **llm) -> PaperClerk:
    settings = Settings(watched_folders=[folder], llm=LLMSettings(vision_model="", **llm))
    clerk = PaperClerk(settings, conn=connect(":memory:"), client_for=models.client_for)
    clerk.scan()
    return clerk


def _current(clerk: PaperClerk):
    doc_id = clerk.list_documents()[0]["id"]
    return repo.current_extraction(clerk.conn, doc_id)


def test_clean_answer_enters_the_library(folder):
    models = FakeModels(claude_sonnet_5_5=[_answer()])
    clerk = _clerk(folder, models)
    r = clerk.classify_pending()
    assert (r.read, r.accepted, r.review, r.failed) == (1, 1, 0, 0)
    row = _current(clerk)
    assert row["needs_review"] == 0 and json.loads(row["review_reasons"]) == []
    assert row["vendor"] == "Origin Energy" and row["amount"] == 236.46
    assert row["due_date"] == "2026-08-05" and row["model"] == "claude-sonnet-5-5"
    assert row["prompt_version"].startswith("classify-") and row["cost_usd"] == 0.004
    assert json.loads(row["evidence_json"])["amount"] == "Amount due $236.46"
    assert clerk.review_queue() == []
    assert clerk.classify_pending().read == 0  # read once, never again


def test_low_confidence_waits_for_review_with_the_reason(folder):
    clerk = _clerk(folder, FakeModels(claude_sonnet_5_5=[_answer(confidence=0.6)]))
    r = clerk.classify_pending()
    assert (r.accepted, r.review) == (0, 1)
    [item] = clerk.review_queue()
    assert item["reasons"] == ["confidence 0.60, below 0.80"] and item["amount"] == 236.46


def test_failed_check_reason_is_stored(folder):
    sloppy = _answer(amount_quote="Total payable $236.46")
    models = FakeModels(claude_sonnet_5_5=[sloppy], claude_opus_5_5=[sloppy])
    clerk = _clerk(folder, models)
    clerk.classify_pending()
    assert models.calls == ["claude-sonnet-5-5", "claude-opus-5-5"]  # second read
    [item] = clerk.review_queue()
    assert item["reasons"] == ["amount: quote not in the document"]


def test_failure_is_tried_once_more_then_waits_for_review(folder):
    down = ["api", "api", "api"]  # retry, then the stronger model: all fail
    models = FakeModels(claude_sonnet_5_5=down[:2] * 2, claude_opus_5_5=down[:1] * 2)
    clerk = _clerk(folder, models)
    assert clerk.classify_pending().failed == 1
    assert clerk.review_queue() == []  # not yet: it gets another try
    assert clerk.classify_pending().failed == 1  # the next run tries again
    [item] = clerk.review_queue()
    assert item["extraction_id"] is None and item["reasons"][0].startswith("no answer")
    assert clerk.classify_pending().read == 0  # then it's left alone
    assert len(models.calls) == 6


def test_failure_then_success_on_the_next_run(folder):
    models = FakeModels(claude_sonnet_5_5=["api", "api", _answer()], claude_opus_5_5=["api"])
    clerk = _clerk(folder, models)
    assert clerk.classify_pending().failed == 1
    assert clerk.classify_pending().accepted == 1
    assert clerk.review_queue() == []


def test_new_prompt_or_model_does_not_reread(folder, monkeypatch):
    models = FakeModels(claude_sonnet_5_5=[_answer()])
    clerk = _clerk(folder, models)
    clerk.classify_pending()
    monkeypatch.setattr("paperclerk.extract.classifier.PROMPT_VERSION", "classify-new")
    clerk.settings.llm.model = "claude-haiku-5-5"
    assert clerk.classify_pending().read == 0


def test_changed_text_makes_old_reads_stale_but_keeps_verified_ones(folder):
    models = FakeModels(claude_sonnet_5_5=[_answer(), _answer(amount=236.46)])
    clerk = _clerk(folder, models)
    clerk.classify_pending()
    doc_id = clerk.list_documents()[0]["id"]
    assert repo.mark_extractions_stale(clerk.conn, doc_id) == 1
    assert repo.documents_to_extract(clerk.conn) == [doc_id]
    assert clerk.classify_pending().read == 1  # read again
    history = clerk.conn.execute(
        "SELECT stale FROM extractions WHERE document_id = ? ORDER BY id", (doc_id,)
    ).fetchall()
    assert [r[0] for r in history] == [1, 0]  # the old read is kept as history

    clerk.conn.execute(
        "UPDATE extractions SET human_verified = 1 WHERE id = ?", (_current(clerk)["id"],)
    )
    assert repo.mark_extractions_stale(clerk.conn, doc_id) == 0
    assert repo.documents_to_extract(clerk.conn) == []  # the user's answer stands


def test_scan_goes_to_the_vision_model_and_waits_for_review(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    Image.new("RGB", (60, 90), "white").save(root / "receipt.jpg")
    models = FakeModels(claude_opus_5_5=[_answer()])
    settings = Settings(watched_folders=[root], llm=LLMSettings())  # vision model: Opus
    clerk = PaperClerk(settings, conn=connect(":memory:"), client_for=models.client_for)
    clerk.scan()
    r = clerk.classify_pending()
    assert models.calls == ["claude-opus-5-5"] and r.review == 1
    [item] = clerk.review_queue()
    assert all("scanned page" in reason for reason in item["reasons"])


def test_changed_image_file_is_skipped_until_rescanned(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    Image.new("RGB", (60, 90), "white").save(root / "receipt.jpg")
    models = FakeModels(claude_opus_5_5=[_answer()])
    clerk = PaperClerk(
        Settings(watched_folders=[root]), conn=connect(":memory:"), client_for=models.client_for
    )
    clerk.scan()
    Image.new("RGB", (60, 90), "black").save(root / "receipt.jpg")  # content changed
    r = clerk.classify_pending()
    assert r.read == 0 and len(r.skipped) == 1 and models.calls == []


def test_one_broken_document_does_not_stop_the_run(folder):
    (folder / "other.txt").write_text(BILL, encoding="utf-8")

    def client_for(name):
        raise RuntimeError("no key")

    settings = Settings(watched_folders=[folder], llm=LLMSettings(vision_model=""))
    clerk = PaperClerk(settings, conn=connect(":memory:"), client_for=client_for)
    clerk.scan()
    r = clerk.classify_pending()
    assert r.read == 1 and r.failed == 1  # two files, same content: one document


# --- the review rule ---------------------------------------------------------------------


def _result(**kw) -> ExtractionResult:
    values = dict(
        extraction=Extraction(doc_type=DocType.INVOICE, confidence=0.9),
        requested_model="m", model="m", prompt_version="v", attempts=(), unread=(),
    )  # fmt: skip
    values.update(kw)
    return ExtractionResult(**values)


def test_review_reasons_in_words():
    assert review_reasons(_result(), 0.8) == []
    r = _result(problems=(Problem("amount", "not in the document"),), unread=("page 3: x",))
    assert review_reasons(r, 0.95) == [
        "confidence 0.90, below 0.95",
        "amount: not in the document",
        "not shown to the model: page 3: x",
    ]
    assert review_reasons(_result(extraction=None), 0.8)[0].startswith("no answer")


# --- review round 1 ---------------------------------------------------------------------


def test_text_changed_during_the_read_is_stored_stale_and_read_again(folder):
    clerk_ref = {}

    class Racing(FakeModels):
        def client_for(self, name):
            inner = super().client_for(name)
            clerk = clerk_ref

            class Client:
                def structured(self, **kw):
                    # a scan with a newer parser changes the pages while the model reads
                    doc_id = clerk["c"].list_documents()[0]["id"]
                    repo.set_parse_result(
                        clerk["c"].conn, doc_id, pages=[Page("Other text")], mime_type="text/plain",
                        parse_status=repo.PARSE_OK, stale_reads=True,
                    )  # fmt: skip
                    return inner.structured(**kw)

            return Client()

    models = Racing(claude_sonnet_5_5=[_answer()])
    clerk = _clerk(folder, models)
    clerk_ref["c"] = clerk
    r = clerk.classify_pending()
    assert r.accepted == 0 and r.read == 1 and "changed while it was read" in r.skipped[0]
    doc_id = clerk.list_documents()[0]["id"]
    assert repo.current_extraction(clerk.conn, doc_id) is None  # nothing in the library
    assert repo.documents_to_extract(clerk.conn) == [doc_id]  # due again


def test_text_change_resets_the_failure_count(folder):
    models = FakeModels(claude_sonnet_5_5=["api"] * 4, claude_opus_5_5=["api"] * 2)
    clerk = _clerk(folder, models)
    clerk.classify_pending()
    clerk.classify_pending()
    doc_id = clerk.list_documents()[0]["id"]
    assert repo.documents_to_extract(clerk.conn) == []  # waiting for review
    repo.set_parse_result(  # a newer parser changed the text
        clerk.conn, doc_id, pages=[Page("new text")], mime_type="text/plain",
        parse_status=repo.PARSE_OK, stale_reads=True,
    )  # fmt: skip
    assert repo.documents_to_extract(clerk.conn) == [doc_id]
    assert clerk.review_queue() == []


def test_scanned_document_without_a_local_file_goes_to_review(tmp_path):
    clerk = PaperClerk(
        Settings(llm=LLMSettings()), conn=connect(":memory:"), client_for=FakeModels().client_for
    )
    doc_id, _ = repo.upsert_document(
        clerk.conn, sha256="x" * 64, source="gmail", mime_type="image/jpeg", size_bytes=10,
        pages=[Page("", needs_vision=True)], parse_status=repo.PARSE_OK,
    )  # fmt: skip
    assert clerk.classify_pending().failed == 1
    assert clerk.classify_pending().failed == 1
    [item] = clerk.review_queue()
    assert "no local file" in item["reasons"][0]
    assert clerk.classify_pending().read == 0


def test_copies_any_path_with_the_content_will_do(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    Image.new("RGB", (60, 90), "white").save(root / "a.jpg")
    (root / "b.jpg").write_bytes((root / "a.jpg").read_bytes())
    models = FakeModels(claude_opus_5_5=[_answer()])
    clerk = PaperClerk(
        Settings(watched_folders=[root]), conn=connect(":memory:"), client_for=models.client_for
    )
    clerk.scan()
    Image.new("RGB", (60, 90), "black").save(root / "a.jpg")  # first copy edited, no rescan
    r = clerk.classify_pending()
    assert r.read == 1 and models.calls == ["claude-opus-5-5"]


def test_storage_error_skips_the_document_and_keeps_going(folder, monkeypatch):
    (folder / "other.txt").write_text(BILL + "\nsecond bill", encoding="utf-8")
    models = FakeModels(claude_sonnet_5_5=[_answer(), _answer()])
    clerk = _clerk(folder, models)
    real = repo.insert_extraction
    calls = []

    def flaky(conn, doc_id, *a, **kw):
        calls.append(1)
        if len(calls) == 1:  # half-written, then the database refuses
            conn.execute(
                "INSERT INTO extraction_failures (document_id, model, prompt_version, error) "
                "VALUES (?, 'm', 'v', 'half')",
                (doc_id,),
            )
            raise sqlite3.OperationalError("database is locked")
        return real(conn, doc_id, *a, **kw)

    monkeypatch.setattr(repo, "insert_extraction", flaky)
    r = clerk.classify_pending()
    assert len(calls) == 2 and r.accepted == 1 and "couldn't be stored" in r.skipped[0]
    assert not clerk.conn.in_transaction
    assert clerk.conn.execute("SELECT COUNT(*) FROM extraction_failures").fetchone()[0] == 0


def test_run_cost_includes_failed_reads(folder):
    class Billed(FakeModels):
        def client_for(self, name):
            class Client:
                def structured(self, **kw):
                    raise LLMError("api", "down", model=name, usage=Usage(10, 10), cost_usd=0.01)

            return Client()

    clerk = _clerk(folder, Billed())
    assert clerk.classify_pending().cost_usd == pytest.approx(0.03)


def test_migration_makes_old_unchecked_answers_stale(monkeypatch):
    from paperclerk.db import connection, init_db

    conn = connect(":memory:")
    all_migrations = connection.MIGRATIONS
    monkeypatch.setattr(connection, "MIGRATIONS", all_migrations[:5])
    init_db(conn)  # a database as the previous release left it
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    conn.execute(
        "INSERT INTO documents (sha256, source, mime_type, parse_status) "
        "VALUES (?, 'gmail', 'text/plain', 'ok')",
        ("y" * 64,),
    )
    conn.execute(
        "INSERT INTO extractions (document_id, doc_type, confidence, needs_review) "
        "VALUES (1, 'invoice', 0.4, 0)"
    )
    conn.commit()
    monkeypatch.setattr(connection, "MIGRATIONS", all_migrations)
    init_db(conn)  # upgrade
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(all_migrations)
    assert repo.current_extraction(conn, 1) is None  # never checked: read again
    assert repo.documents_to_extract(conn) == [1]
