"""F2 step 2: ask the model to classify a document and extract its fields, by the labeling rules.

When a call gives no usable answer (refused, cut off, not in the schema, an API error after the
SDK's own retries), it is tried once more on the same model, then once on the next stronger
model. If that fails too, the result is a failed call: the document goes to review and nothing
is stored as its answer. A request the API rejects as malformed (400, 401, ...) isn't retried:
no model would take it. (The cost: a 400 that only one model gives, such as an effort level it
doesn't accept, doesn't move on to the stronger model either.) Every attempt is kept with
its cost, so evals see what retries cost and how often the stronger model had to step in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from paperclerk.extract.inputs import ModelInput, build_input
from paperclerk.extract.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from paperclerk.extract.schemas import Extraction, ModelAnswer
from paperclerk.ingest.parsers.base import ParsedDocument
from paperclerk.llm.base import Effort, LLMClient, LLMError, Usage
from paperclerk.llm.pricing import base_model

# The model to fall back to when a model fails twice. The strongest has none: it gets its
# retry and no third attempt.
STRONGER: dict[str, str] = {
    "claude-haiku-5-5": "claude-sonnet-5-5",
    "claude-sonnet-5-5": "claude-opus-5-5",
}


@dataclass(frozen=True)
class Attempt:
    asked: str  # the model this attempt asked for
    model: str  # the model that answered, as the API reported it (undated)
    error: str | None  # None: this attempt gave the answer
    usage: Usage | None  # None: the call never reached the model
    latency_ms: int
    cost_usd: float | None


@dataclass(frozen=True)
class ExtractionResult:
    extraction: Extraction | None  # None: every attempt failed
    requested_model: str
    model: str | None  # the model whose answer this is
    prompt_version: str
    attempts: tuple[Attempt, ...]
    unread: tuple[str, ...]  # parts of the document the model wasn't shown (see inputs.py)

    @property
    def failed(self) -> bool:
        return self.extraction is None

    @property
    def fell_back(self) -> bool:
        """Answered by the stronger model, not the one asked for."""
        return not self.failed and self.attempts[-1].asked != self.requested_model

    @property
    def cost_usd(self) -> float | None:
        """All attempts together. An attempt that never reached the model (no usage) cost
        nothing; one whose model has no known price makes the total unknown (None)."""
        total = 0.0
        for a in self.attempts:
            if a.usage is None:
                continue
            if a.cost_usd is None:
                return None
            total += a.cost_usd
        return total

    @property
    def latency_ms(self) -> int:
        return sum(a.latency_ms for a in self.attempts)


def attempt_plan(model: str) -> list[str]:
    """[model, model, stronger model] — the stronger one only if there is one."""
    plan = [model, model]
    stronger = STRONGER.get(base_model(model))
    if stronger:
        plan.append(stronger)
    return plan


def extract(
    doc: ParsedDocument,
    data: bytes,
    file_name: str,
    *,
    model: str,
    client_for: Callable[[str], LLMClient],
    effort: Effort | None = None,
) -> ExtractionResult:
    """`data` is the file's bytes, the same ones `doc` was parsed from. `client_for` gives a
    client for a model name (llm.anthropic_client.client_for; tests pass a fake one)."""
    model_input = build_input(doc, data, file_name)
    return extract_input(model_input, model=model, client_for=client_for, effort=effort)


def extract_input(
    model_input: ModelInput,
    *,
    model: str,
    client_for: Callable[[str], LLMClient],
    effort: Effort | None = None,
) -> ExtractionResult:
    attempts: list[Attempt] = []
    images = [(img.media_type, img.data) for img in model_input.images]

    def result(extraction: Extraction | None, answered_by: str | None) -> ExtractionResult:
        return ExtractionResult(
            extraction, model, answered_by, PROMPT_VERSION, tuple(attempts), model_input.unread
        )

    for name in attempt_plan(model):
        try:
            answer = client_for(name).structured(
                system=SYSTEM_PROMPT,
                user_text=model_input.text,
                schema=ModelAnswer,
                images=images,
                effort=effort,
            )
        except LLMError as e:
            attempts.append(Attempt(name, e.model, str(e), e.usage, e.latency_ms, e.cost_usd))
            if e.kind == "rejected":  # the request itself is wrong: no model will take it
                break
            continue
        attempts.append(
            Attempt(name, answer.model, None, answer.usage, answer.latency_ms, answer.cost_usd)
        )
        return result(answer.value.to_extraction(), answer.model)
    return result(None, None)
