"""F2 step 2: prompt, Claude client and the retry / stronger-model rule. No API calls."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
from pydantic import ValidationError

from paperclerk.extract.classifier import attempt_plan, extract
from paperclerk.extract.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from paperclerk.extract.schemas import DocType, Extraction, ModelAnswer
from paperclerk.ingest.parsers import Page
from paperclerk.ingest.parsers.base import ParsedDocument
from paperclerk.llm.anthropic_client import AnthropicClient
from paperclerk.llm.base import LLMError, LLMResult, Usage
from paperclerk.llm.pricing import cost_usd

ROOT = Path(__file__).parent.parent
BILL = ParsedDocument("application/pdf", (Page("Origin Energy\nAmount due $64.38"),))
ANSWER = ModelAnswer(
    doc_type=DocType.INVOICE,
    reasoning="asks for payment by 6 Feb",
    evidence={"vendor": "Origin Energy", "amount": "Amount due $64.38", "issue_date": None,
              "due_date": None},
    vendor="Origin Energy", amount=64.38, currency="AUD", issue_date=None, due_date=None,
    is_recurring=None, period=None, confidence=0.9,
)  # fmt: skip
ANSWER_JSON = ANSWER.model_dump_json()


# --- prompt ------------------------------------------------------------------------------


def test_prompt_rules_are_the_labeling_guide_word_for_word():
    guide = (ROOT / "evals" / "labeling-guide.md").read_text()
    rules = (ROOT / "src/paperclerk/extract/prompts/rules.md").read_text()
    expected = guide[guide.index("## 1. Document type") : guide.index("## 6. ")].rstrip() + "\n"
    assert rules == expected, "copy sections 1-5 of the labeling guide into prompts/rules.md"
    assert expected.strip() in SYSTEM_PROMPT
    assert "{rules}" not in SYSTEM_PROMPT


def test_prompt_version_follows_the_text():
    assert PROMPT_VERSION.startswith("classify-") and len(PROMPT_VERSION) == len("classify-") + 10


# --- pricing -----------------------------------------------------------------------------


def test_cost_from_usage():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5-5", u) == pytest.approx(2.0 + 1.0 + 0.1)
    assert cost_usd("claude-haiku-5-5", Usage(input_tokens=2000, output_tokens=500)) == (
        pytest.approx((2000 * 0.10 + 500 * 0.50) / 1e6)
    )
    assert cost_usd("claude-sonnet-5-5", Usage(cache_write_tokens=1_000_000)) == pytest.approx(2.5)
    assert cost_usd("some-new-model", Usage(input_tokens=10)) is None


def test_haiku_long_prompt_price():
    u = Usage(input_tokens=150_000, output_tokens=1000)
    assert cost_usd("claude-haiku-5-5", u) == pytest.approx((150_000 * 0.50 + 1000 * 2.50) / 1e6)


# --- Anthropic client (fake SDK) -----------------------------------------------------------


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _response(stop="end_turn", text=ANSWER_JSON, model="claude-sonnet-5-5"):
    usage = SimpleNamespace(
        input_tokens=1200, output_tokens=300, cache_read_input_tokens=3000,
        cache_creation_input_tokens=0,
    )  # fmt: skip
    return SimpleNamespace(
        stop_reason=stop, usage=usage, model=model,
        content=[SimpleNamespace(type="text", text=text)] if text is not None else [],
        stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
    )  # fmt: skip


def _client(**kw):
    messages = FakeMessages(**kw)
    return AnthropicClient("claude-sonnet-5-5", sdk_client=SimpleNamespace(messages=messages)), (
        messages
    )


def test_client_sends_images_first_caches_the_prompt_and_reports_cost():
    client, messages = _client(response=_response())
    result = client.structured(
        system="RULES", user_text="DOC", schema=ModelAnswer, images=[("image/jpeg", b"\xff\xd8")],
        effort="low",
    )  # fmt: skip
    sent = messages.calls[0]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = sent["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "text"]
    assert content[0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "/9g="}
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"] == {
        "type": "json_schema", "schema": anthropic.transform_schema(ModelAnswer),
    }  # fmt: skip
    assert "tool_choice" not in sent and "thinking" not in sent
    assert result.value == ANSWER and result.usage.cache_read_tokens == 3000
    assert result.cost_usd == pytest.approx((1200 * 2 + 300 * 10 + 3000 * 0.1) / 1e6)


def test_client_leaves_effort_to_the_model_when_unset():
    client, messages = _client(response=_response())
    client.structured(system="R", user_text="D", schema=ModelAnswer)
    assert "effort" not in messages.calls[0]["output_config"]


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (_response(stop="refusal", text="I can't help with that."), "refusal"),
        (_response(stop="max_tokens", text='{"doc_type":"inv'), "truncated"),
        (_response(text=None), "invalid"),
        (_response(text=ANSWER_JSON.replace('"confidence":0.9', '"confidence":1.5')), "invalid"),
        (
            _response(
                text=ANSWER_JSON.replace('"due_date":null,"is', '"due_date":"2026-02-30","is')
            ),
            "invalid",
        ),
    ],
)
def test_client_errors_keep_what_the_call_cost(response, kind):
    client, _ = _client(response=response)
    with pytest.raises(LLMError) as e:
        client.structured(system="R", user_text="D", schema=ModelAnswer)
    assert e.value.kind == kind and e.value.usage.output_tokens == 300  # billed, so counted
    assert e.value.cost_usd is not None


def _status_error(code: int):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(code, request=request, json={"error": {"message": "x"}})
    return anthropic.APIStatusError("x", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), "api"),
        (_status_error(429), "api"),
        (_status_error(529), "api"),
        (_status_error(400), "rejected"),
        (_status_error(401), "rejected"),
        (_status_error(413), "rejected"),
    ],
)
def test_client_api_errors(error, kind):
    client, _ = _client(error=error)
    with pytest.raises(LLMError) as e:
        client.structured(system="R", user_text="D", schema=ModelAnswer)
    assert e.value.kind == kind and e.value.usage is None


def test_dated_model_name_is_the_same_model():
    client, _ = _client(response=_response(model="claude-sonnet-5-5-20261001"))
    result = client.structured(system="R", user_text="D", schema=ModelAnswer)
    assert result.model == "claude-sonnet-5-5" and result.cost_usd is not None
    assert attempt_plan("claude-sonnet-5-5-20261001")[-1] == "claude-opus-5-5"


# --- retry, then the stronger model --------------------------------------------------------


class FakeModel:
    """Fails `fail` times, then answers. Shared across names so the order of calls is kept."""

    def __init__(self, log: list[str], outcomes: dict[str, list[str]]):
        self.log, self.outcomes = log, outcomes

    def client_for(self, name: str):
        model = self

        class Client:
            def structured(self, **kwargs):
                model.log.append(name)
                assert kwargs["system"] == SYSTEM_PROMPT and kwargs["schema"] is ModelAnswer
                outcome = model.outcomes[name].pop(0)
                if outcome != "ok":
                    raise LLMError(
                        outcome, "boom", model=name, usage=Usage(10, 5), latency_ms=100,
                        cost_usd=0.001,
                    )  # fmt: skip
                return LLMResult(ANSWER, name + "-20261001", Usage(1000, 300), 900, 0.01)

        c = Client()
        c.model = name
        return c


def _run(model: str, outcomes: dict[str, list[str]]):
    log: list[str] = []
    result = extract(
        BILL, b"", "bill.pdf", model=model, client_for=FakeModel(log, outcomes).client_for
    )
    return result, log


def test_first_try_answers():
    result, log = _run("claude-sonnet-5-5", {"claude-sonnet-5-5": ["ok"]})
    assert log == ["claude-sonnet-5-5"] and result.extraction == ANSWER.to_extraction()
    assert result.extraction.evidence.amount == "Amount due $64.38"
    assert not result.failed and not result.fell_back
    assert result.prompt_version == PROMPT_VERSION and result.cost_usd == pytest.approx(0.01)


def test_retry_on_the_same_model_first():
    result, log = _run("claude-sonnet-5-5", {"claude-sonnet-5-5": ["invalid", "ok"]})
    assert log == ["claude-sonnet-5-5"] * 2 and result.model == "claude-sonnet-5-5-20261001"
    assert not result.fell_back and result.cost_usd == pytest.approx(0.011)
    assert result.latency_ms == 1000


def test_then_the_next_stronger_model():
    result, log = _run(
        "claude-haiku-5-5", {"claude-haiku-5-5": ["refusal", "api"], "claude-sonnet-5-5": ["ok"]}
    )
    assert log == ["claude-haiku-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"]
    assert result.model.startswith("claude-sonnet-5-5") and result.fell_back


def test_then_a_failed_call():
    result, log = _run(
        "claude-sonnet-5-5",
        {"claude-sonnet-5-5": ["truncated", "truncated"], "claude-opus-5-5": ["invalid"]},
    )
    assert log == ["claude-sonnet-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
    assert result.failed and result.extraction is None and result.model is None
    assert [a.error.split(":")[0] for a in result.attempts] == ["truncated", "truncated", "invalid"]
    assert result.cost_usd == pytest.approx(0.003)  # failed calls still cost money


def test_strongest_model_only_retries():
    assert attempt_plan("claude-opus-5-5") == ["claude-opus-5-5"] * 2
    assert attempt_plan("claude-haiku-5-5") == [
        "claude-haiku-5-5", "claude-haiku-5-5", "claude-sonnet-5-5",
    ]  # fmt: skip
    result, log = _run("claude-opus-5-5", {"claude-opus-5-5": ["api", "api"]})
    assert log == ["claude-opus-5-5"] * 2 and result.failed


def test_unread_pages_are_passed_on():
    doc = ParsedDocument("image/jpeg", (Page("", needs_vision=True),))
    log: list[str] = []
    result = extract(
        doc, b"not an image", "x.jpg", model="claude-opus-5-5",
        client_for=FakeModel(log, {"claude-opus-5-5": ["ok"]}).client_for,
    )  # fmt: skip
    assert result.unread == ("page 1: scanned, but no image could be made from it",)


def test_rejected_request_is_not_retried():
    result, log = _run("claude-haiku-5-5", {"claude-haiku-5-5": ["rejected"]})
    assert log == ["claude-haiku-5-5"] and result.failed


def test_model_must_fill_every_field_even_if_null():
    required = anthropic.transform_schema(ModelAnswer)["required"]
    assert required[:3] == ["doc_type", "reasoning", "evidence"]  # written before the values
    assert {"vendor", "amount", "due_date", "confidence"} <= set(required)
    with pytest.raises(ValidationError):
        ModelAnswer.model_validate_json(ANSWER_JSON.replace(',"currency":"AUD"', ""))


def test_evidence_is_part_of_the_answer_schema():
    answer = Extraction.model_validate(
        {
            "doc_type": "invoice",
            "amount": 236.46,
            "confidence": 0.9,
            "evidence": {"amount": "Amount due $236.46"},
        }
    )
    assert answer.evidence.amount == "Amount due $236.46" and answer.evidence.vendor is None
