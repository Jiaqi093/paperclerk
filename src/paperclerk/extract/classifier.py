"""F2 steps 2-3: ask the model to classify a document and extract its fields by the labeling
rules, then check the answer against the document.

Which model: the configured one, or the vision model (a stronger one) when the document has
scanned pages, since reading images is harder.

When a call gives no usable answer (refused, cut off, not in the schema, an API error after the
SDK's own retries), it is tried once more on the same model, then once on the next stronger
model. If that fails too, the result is a failed call: the document goes to review and nothing
is stored as its answer. A request the API rejects as malformed (400, 401, ...) isn't retried:
no model would take it. (The cost: a 400 that only one model gives, such as an effort level it
doesn't accept, doesn't move on to the stronger model either.)

When an answer fails the source or evidence check (verify.py), the next stronger model reads
the document once more, unless no page has text: no model's answer can be checked there. The
second answer is kept if it is no worse: a value not in the document counts three times a quote
problem, and so does a value the first answer gave and the second left out (an empty answer has
no problems, but isn't better). On a tie the stronger reader wins. Whichever is kept, a company,
amount or date the first read found in the document and the second read changed or left out is
a problem of its own: two models reading the same page differently is a reason for a person to
look. (A first value that wasn't in the document at all being replaced is the point of the
second read.)

Every attempt is kept with its cost, so evals see what retries and second reads cost.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from paperclerk.extract import verify
from paperclerk.extract.inputs import ModelInput, build_input
from paperclerk.extract.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from paperclerk.extract.schemas import Extraction, ModelAnswer
from paperclerk.extract.source_check import LITERAL_FIELDS
from paperclerk.ingest.parsers.base import ParsedDocument
from paperclerk.llm.base import Effort, LLMClient, LLMError, Usage
from paperclerk.llm.pricing import base_model

# The next stronger model. The strongest has none.
STRONGER: dict[str, str] = {
    "claude-haiku-5-5": "claude-sonnet-5-5",
    "claude-sonnet-5-5": "claude-opus-5-5",
}


@dataclass(frozen=True)
class Attempt:
    asked: str  # the model this attempt asked for
    model: str  # the model that answered, as the API reported it (undated)
    error: str | None  # None: this attempt gave an answer
    usage: Usage | None  # None: the call never reached the model
    latency_ms: int
    cost_usd: float | None
    second_read: bool = False  # asked because the first answer failed the check


@dataclass(frozen=True)
class ExtractionResult:
    extraction: Extraction | None  # None: every attempt failed
    requested_model: str  # the model this document was meant for (vision model for scans)
    model: str | None  # the model whose answer this is
    prompt_version: str
    attempts: tuple[Attempt, ...]
    unread: tuple[str, ...]  # parts of the document the model wasn't shown (see inputs.py)
    problems: tuple[verify.Problem, ...] = ()  # the kept answer's source/evidence problems
    answered_by: str | None = None  # the model asked for the kept answer
    cached: bool = False  # reused from an earlier identical read: no calls, no cost
    # For a cached result: what the original read cost and took (evals report the cost of a
    # read, not of this run). None otherwise.
    original_cost_usd: float | None = None
    original_latency_ms: int | None = None

    @property
    def failed(self) -> bool:
        return self.extraction is None

    def to_json(self) -> str:
        """The answer and its checks, for the cache. Attempts aren't kept: a reuse costs
        nothing, and the calls that produced it are in llm_calls."""
        import json

        return json.dumps(
            {
                "extraction": self.extraction.model_dump(mode="json") if self.extraction else None,
                "requested_model": self.requested_model,
                "model": self.model,
                "prompt_version": self.prompt_version,
                "unread": list(self.unread),
                "problems": [[p.field, p.what, p.scanned] for p in self.problems],
                "answered_by": self.answered_by,
                "cost_usd": self.cost_usd if not self.cached else self.original_cost_usd,
                "latency_ms": self.latency_ms if not self.cached else self.original_latency_ms,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> ExtractionResult:
        import json

        d = json.loads(text)
        extraction = Extraction.model_validate(d["extraction"]) if d["extraction"] else None
        return cls(
            extraction, d["requested_model"], d["model"], d["prompt_version"], (),
            tuple(d["unread"]), tuple(verify.Problem(*p) for p in d["problems"]),
            d["answered_by"], cached=True, original_cost_usd=d.get("cost_usd"),
            original_latency_ms=d.get("latency_ms"),
        )  # fmt: skip

    @property
    def fell_back(self) -> bool:
        """The kept answer came from a stronger model than the one meant for this document."""
        return self.answered_by is not None and self.answered_by != self.requested_model

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


def stronger(model: str) -> str | None:
    return STRONGER.get(base_model(model))


def attempt_plan(model: str) -> list[str]:
    """[model, model, stronger model] — the stronger one only if there is one."""
    plan = [model, model]
    if stronger(model):
        plan.append(stronger(model))
    return plan


def extract(
    doc: ParsedDocument,
    data: bytes,
    file_name: str,
    *,
    model: str,
    client_for: Callable[[str], LLMClient],
    vision_model: str | None = None,
    effort: Effort | None = None,
) -> ExtractionResult:
    """`data` is the file's bytes, the same ones `doc` was parsed from. `client_for` gives a
    client for a model name (llm.anthropic_client.client_for; tests pass a fake one)."""
    model_input = build_input(doc, data, file_name)
    if model_input.images and vision_model:
        model = vision_model
    return extract_input(
        model_input,
        doc.text,
        has_scanned_pages=doc.needs_vision,
        has_text_pages=any(p.text.strip() and not p.needs_vision for p in doc.pages),
        model=model,
        client_for=client_for,
        effort=effort,
    )


def extract_input(
    model_input: ModelInput,
    text: str,
    *,
    has_scanned_pages: bool,
    has_text_pages: bool = True,
    model: str,
    client_for: Callable[[str], LLMClient],
    effort: Effort | None = None,
) -> ExtractionResult:
    """`text` is the document's parsed text, which the answer is checked against."""
    attempts: list[Attempt] = []
    images = [(img.media_type, img.data) for img in model_input.images]

    def ask(name: str, second_read: bool = False) -> tuple[Extraction | None, str, str | None]:
        """One call. Returns (answer or None, model asked, error kind or None)."""
        try:
            answer = client_for(name).structured(
                system=SYSTEM_PROMPT,
                user_text=model_input.text,
                schema=ModelAnswer,
                images=images,
                effort=effort,
            )
        except LLMError as e:
            attempts.append(
                Attempt(name, e.model, str(e), e.usage, e.latency_ms, e.cost_usd, second_read)
            )
            return None, name, e.kind
        attempts.append(
            Attempt(
                name, answer.model, None, answer.usage, answer.latency_ms, answer.cost_usd,
                second_read,
            )
        )  # fmt: skip
        return answer.value.to_extraction(), answer.model, None

    def result(
        extraction: Extraction | None,
        answered: str | None,
        problems: list[verify.Problem],
        asked: str | None,
    ) -> ExtractionResult:
        return ExtractionResult(
            extraction, model, answered, PROMPT_VERSION, tuple(attempts), model_input.unread,
            tuple(problems), asked,
        )  # fmt: skip

    # 1. Get an answer: retry, then the stronger model.
    kept: Extraction | None = None
    answered = asked = None
    for name in attempt_plan(model):
        kept, answered, error = ask(name)
        if kept is not None:
            asked = name
            break
        answered = None
        if error == "rejected":  # the request itself is wrong: no model will take it
            break
    if kept is None:
        return result(None, None, [], None)

    # 2. Check it against the document; on a fixable problem, one more read by a stronger model.
    problems = verify.check(kept, text, has_scanned_pages=has_scanned_pages)
    better = stronger(asked)
    checkable = verify.can_retry(problems) or has_text_pages
    if problems and checkable and better:
        second, second_model, _ = ask(better, second_read=True)
        if second is not None:
            second_problems = verify.check(second, text, has_scanned_pages=has_scanned_pages)
            dropped = sum(
                1
                for f in LITERAL_FIELDS
                if getattr(kept, f) is not None and getattr(second, f) is None
            )
            second_score = verify.badness(second_problems) + dropped * verify.DROPPED_WEIGHT
            first, first_problems = kept, problems
            if second_score <= verify.badness(problems):
                kept, answered, asked, problems = second, second_model, better, second_problems
            problems = problems + _disagreements(first, first_problems, second)
    return result(kept, answered, problems, asked)


def _disagreements(
    first: Extraction, first_problems: list[verify.Problem], second: Extraction
) -> list[verify.Problem]:
    """Values the first read found in the document that the second read changed or left out.
    A first value that wasn't in the document at all is what the second read is for: replacing
    it is the fix, not a disagreement."""
    unfounded = {p.field for p in first_problems if p.what == "not in the document"}
    found = []
    for f in LITERAL_FIELDS:
        if f in unfounded:
            continue
        a, b = getattr(first, f), getattr(second, f)
        if a is not None and b is None:
            found.append(verify.Problem(f, "the second read left this out"))
        elif a is not None and b is not None and not verify.same_value(f, a, b):
            found.append(verify.Problem(f, "the two reads disagree"))
    return found


def attempt_purpose(result: ExtractionResult, index: int) -> str:
    """Why attempt `index` was made, for the call log: read, retry, stronger model, second
    read."""
    attempt = result.attempts[index]
    if attempt.second_read:
        return "second read"
    if index == 0:
        return "read"
    if attempt.asked == result.attempts[0].asked:
        return "retry"
    return "stronger model"
