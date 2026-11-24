"""F2 step 5: call log, reuse of identical reads, cost estimate and budget, eval predictions.
No API calls."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paperclerk import cli
from paperclerk.config import LLMSettings, Settings, load_env
from paperclerk.core import PaperClerk
from paperclerk.db import connect, init_db, repo
from paperclerk.extract import cache
from paperclerk.extract.estimate import document_cost
from paperclerk.extract.schemas import DocType, ModelAnswer
from paperclerk.ingest.parsers import Page
from paperclerk.ingest.parsers.base import ParsedDocument
from paperclerk.llm.base import LLMError, LLMResult, Usage

ROOT = Path(__file__).parent.parent
BILL = "Origin Energy\nAmount due $236.46\nNew charges due 05 Aug 2026"


def _answer(amount_quote="Amount due $236.46") -> ModelAnswer:
    return ModelAnswer(
        doc_type=DocType.INVOICE, reasoning="a bill", vendor="Origin Energy", amount=236.46,
        currency="AUD", issue_date=None, due_date=date(2026, 8, 5), is_recurring=True,
        period="monthly", confidence=0.95,
        evidence={"vendor": "Origin Energy", "amount": amount_quote, "issue_date": None,
                  "due_date": "New charges due 05 Aug 2026"},
    )  # fmt: skip


class Models:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def client_for(self, name):
        models = self

        class Client:
            def structured(self, **kw):
                models.calls.append(name)
                outcome = models.outcomes.pop(0) if models.outcomes else _answer()
                if isinstance(outcome, str):
                    raise LLMError(outcome, "down", model=name, usage=Usage(5, 5), cost_usd=0.001)
                return LLMResult(outcome, name, Usage(1000, 200, 3000), 700, 0.01)

        return Client()


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    (root / "bill.txt").write_text(BILL, encoding="utf-8")
    return root


def _clerk(folder, models, **llm):
    settings = Settings(watched_folders=[folder], llm=LLMSettings(vision_model="", **llm))
    clerk = PaperClerk(settings, conn=connect(":memory:"), client_for=models.client_for)
    clerk.scan()
    return clerk


# --- call log ------------------------------------------------------------------------------


def test_every_call_is_logged_with_its_purpose_and_cost(folder):
    models = Models("api", _answer(amount_quote="made up $236.46"), _answer())
    clerk = _clerk(folder, models)
    clerk.classify_pending()
    rows = clerk.conn.execute(
        "SELECT purpose, asked_model, cost_usd, error IS NOT NULL AS failed, cache_read_tokens "
        "FROM llm_calls ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("read", "claude-sonnet-5-5", 0.001, 1, 0),
        ("retry", "claude-sonnet-5-5", 0.01, 0, 3000),
        ("second read", "claude-opus-5-5", 0.01, 0, 3000),
    ]
    by_model = {r["model"]: r for r in clerk.usage()}
    assert by_model["claude-sonnet-5-5"]["calls"] == 2
    assert by_model["claude-sonnet-5-5"]["failed"] == 1
    assert by_model["claude-opus-5-5"]["calls"] == 1
    assert repo.spent_since(clerk.conn, repo.month_start()) == pytest.approx(0.021)


# --- reuse ---------------------------------------------------------------------------------


def test_identical_read_is_reused_for_free(folder):
    models = Models()
    clerk = _clerk(folder, models)
    clerk.classify_pending()
    doc_id = clerk.list_documents()[0]["id"]
    # the same text comes back from a newer parser: the old read is stale, but identical
    repo.set_parse_result(
        clerk.conn, doc_id, pages=[Page(BILL)], mime_type="text/plain",
        parse_status=repo.PARSE_OK, stale_reads=True,
    )  # fmt: skip
    r = clerk.classify_pending()
    assert (r.read, r.reused, r.accepted, r.cost_usd) == (1, 1, 1, 0)
    assert len(models.calls) == 1  # no second call


def test_cache_key_changes_with_anything_that_decides_the_answer():
    pages = [Page(BILL)]
    base = dict(model="m", vision_model="v", effort="low")
    key = cache.cache_key("s", pages, **base)
    assert key == cache.cache_key("s", pages, **base)
    assert key != cache.cache_key("t", pages, **base)
    assert key != cache.cache_key("s", [Page(BILL + "!")], **base)
    assert key != cache.cache_key("s", pages, **{**base, "effort": "medium"})
    assert key != cache.cache_key("s", pages, **{**base, "model": "n"})
    assert key != cache.cache_key("s", pages, **{**base, "vision_model": None})
    assert key != cache.cache_key("s", pages, **base, prompt_version="classify-other")


def test_failed_reads_are_not_cached():
    conn = connect(":memory:")
    init_db(conn)
    doc = ParsedDocument("text/plain", (Page(BILL),))
    models = Models("api", "api", "api", _answer())
    kw = dict(sha256="s", model="claude-sonnet-5-5", vision_model=None, effort=None)
    first = cache.extract_cached(conn, doc, b"", "b.txt", client_for=models.client_for, **kw)
    assert first.failed
    second = cache.extract_cached(conn, doc, b"", "b.txt", client_for=models.client_for, **kw)
    assert not second.failed and not second.cached
    third = cache.extract_cached(conn, doc, b"", "b.txt", client_for=models.client_for, **kw)
    assert third.cached and third.extraction == second.extraction and third.cost_usd == 0
    assert len(models.calls) == 4


# --- estimate and budget -------------------------------------------------------------------


def test_estimate_is_on_the_high_side_and_scans_cost_more():
    text = ParsedDocument("application/pdf", (Page(BILL),))
    scan = ParsedDocument("image/jpeg", (Page("", needs_vision=True),))
    t = document_cost(text, model="claude-sonnet-5-5", vision_model=None)
    s = document_cost(scan, model="claude-sonnet-5-5", vision_model="claude-opus-5-5")
    assert 0.01 < t < s < 0.3  # a scan on Opus: about $0.14 with the margin
    assert document_cost(text, model="new-model", vision_model=None) > 0  # never free


def test_estimate_counts_reused_reads_as_free(folder):
    clerk = _clerk(folder, Models())
    count, usd = clerk.estimate_pending()
    assert count == 1 and usd > 0
    clerk.classify_pending()
    assert clerk.estimate_pending() == (0, 0.0)


def test_monthly_budget_stops_the_run_before_spending(folder):
    models = Models()
    clerk = _clerk(folder, models, monthly_budget_usd=0.001)
    r = clerk.classify_pending()
    assert r.read == 0 and models.calls == [] and "monthly budget" in r.stopped


def test_cli_asks_above_the_threshold(folder, monkeypatch):
    models = Models()
    clerk = _clerk(folder, models, confirm_above_usd=0.0)
    monkeypatch.setattr(cli, "_clerk", lambda config: clerk)
    no = CliRunner().invoke(cli.app, ["extract"], input="n\n")
    assert no.exit_code == 1 and models.calls == []
    yes = CliRunner().invoke(cli.app, ["extract"], input="y\n")
    assert yes.exit_code == 0 and "library=1" in yes.output and len(models.calls) == 1
    assert "nothing to read" in CliRunner().invoke(cli.app, ["extract"]).output
    assert "total this month" in CliRunner().invoke(cli.app, ["usage"]).output


def test_cli_below_the_threshold_does_not_ask(folder, monkeypatch):
    clerk = _clerk(folder, Models())
    monkeypatch.setattr(cli, "_clerk", lambda config: clerk)
    out = CliRunner().invoke(cli.app, ["extract"])
    assert out.exit_code == 0 and "library=1" in out.output


# --- .env ----------------------------------------------------------------------------------


def test_env_file_fills_in_but_never_overrides(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "# comment\nPAPERCLERK_T1=from-file\nexport PAPERCLERK_T2='quoted'\nPAPERCLERK_T3=x\n"
    )
    monkeypatch.setenv("PAPERCLERK_T3", "already set")
    monkeypatch.delenv("PAPERCLERK_T1", raising=False)
    monkeypatch.delenv("PAPERCLERK_T2", raising=False)
    load_env(tmp_path / ".env")
    assert os.environ["PAPERCLERK_T1"] == "from-file"
    assert os.environ["PAPERCLERK_T2"] == "quoted"
    assert os.environ["PAPERCLERK_T3"] == "already set"


# --- eval predictions ----------------------------------------------------------------------


def _predict_module():
    spec = importlib.util.spec_from_file_location("predict", ROOT / "evals" / "predict.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_predict_writes_what_run_eval_scores_and_reuses_reads(tmp_path, monkeypatch):
    (tmp_path / "bill.txt").write_text(BILL, encoding="utf-8")
    (tmp_path / "labels.jsonl").write_text(
        json.dumps({"file": "bill.txt", "doc_type": "invoice", "vendor": "Origin Energy",
                    "amount": 236.46, "due_date": "2026-08-05"}) + "\n"
    )  # fmt: skip
    models = Models()
    monkeypatch.setattr("paperclerk.llm.anthropic_client.client_for", models.client_for)
    argv = ["predict", "--labels", str(tmp_path / "labels.jsonl"), "--cache",
            str(tmp_path / "cache.sqlite"), "--config", str(tmp_path / "none.toml")]  # fmt: skip
    monkeypatch.setattr(sys, "argv", argv)
    module = _predict_module()
    module.main()
    module.main()  # second run: reused
    assert len(models.calls) == 1
    [row] = [json.loads(x) for x in (tmp_path / "predictions.jsonl").read_text().splitlines()]
    assert row["output"]["amount"] == 236.46
    assert row["cost_usd"] == 0.01  # what the read cost, though this run reused it
    assert row["output"]["evidence"]["amount"] == "Amount due $236.46"

    from paperclerk.evals.data import load_labels, load_predictions, read_texts
    from paperclerk.evals.score import score

    labels = load_labels(tmp_path / "labels.jsonl")
    report = score(labels, load_predictions(tmp_path / "predictions.jsonl"),
                   read_texts(labels, tmp_path), 0.8)  # fmt: skip
    assert report.fully_correct == 1 and report.red_line_ok


# --- review round 1 ---------------------------------------------------------------------


def test_a_change_in_the_checking_code_is_not_served_from_the_cache(monkeypatch):
    pages = [Page(BILL)]
    kw = dict(model="m", vision_model=None, effort=None)
    before = cache.cache_key("s", pages, **kw)
    monkeypatch.setattr(cache, "PIPELINE_VERSION", "other-checks")
    assert cache.cache_key("s", pages, **kw) != before
    assert cache.cache_key("s", pages, file_name="a.pdf", **kw) != cache.cache_key(
        "s", pages, file_name="b.pdf", **kw
    )


def test_budget_refuses_a_model_without_a_price(folder):
    models = Models()
    clerk = _clerk(folder, models, model="claude-unknown-9", monthly_budget_usd=5.0)
    r = clerk.classify_pending()
    assert r.read == 0 and models.calls == [] and "no price known" in r.stopped


def test_paid_calls_are_kept_even_if_storing_the_answer_fails(folder, monkeypatch):
    import sqlite3

    models = Models()
    clerk = _clerk(folder, models)
    real = repo.insert_extraction

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "insert_extraction", locked)
    r = clerk.classify_pending()
    assert r.accepted == 0 and "couldn't be stored" in r.skipped[0]
    assert clerk.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
    monkeypatch.setattr(repo, "insert_extraction", real)
    r = clerk.classify_pending()  # the next run reuses the read: no second payment
    assert r.accepted == 1 and r.reused == 1 and len(models.calls) == 1


def test_broken_cache_entry_is_a_miss():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO extraction_cache (key, result_json, created_at) VALUES ('k', '{}', 'now')"
    )
    assert cache.get(conn, "k") is None


def test_env_file_inline_comments_and_bom(tmp_path, monkeypatch):
    (tmp_path / ".env").write_bytes(
        '\ufeffPAPERCLERK_B1=key-1 # work account\nPAPERCLERK_B2="quoted # kept" # c\n'.encode()
    )
    for k in ("PAPERCLERK_B1", "PAPERCLERK_B2"):
        monkeypatch.delenv(k, raising=False)
    load_env(tmp_path / ".env")
    assert os.environ["PAPERCLERK_B1"] == "key-1"
    assert os.environ["PAPERCLERK_B2"] == "quoted # kept"


def test_pipeline_version_follows_the_checking_code(monkeypatch):
    from importlib import resources

    real = resources.files

    class Edited:
        def __init__(self, package):
            self.package = real(package)

        def joinpath(self, name):
            inner = self.package.joinpath(name)
            if name != "verify.py":
                return inner

            class File:
                def read_bytes(self):
                    return inner.read_bytes() + b"\n# a new check"

            return File()

    before = cache._pipeline_version()
    monkeypatch.setattr(resources, "files", Edited)
    assert cache._pipeline_version() != before
