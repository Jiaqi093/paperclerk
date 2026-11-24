from __future__ import annotations

import base64
import time
from collections.abc import Sequence
from functools import cache, lru_cache
from typing import Any

from pydantic import ValidationError

from paperclerk.llm.base import Effort, LLMError, LLMResult, T, Usage
from paperclerk.llm.pricing import base_model, cost_usd

# Room for thinking plus the answer; an extraction answer is ~500 tokens. At high effort
# thinking can run out of room: watch the truncation count in evals before raising effort.
MAX_TOKENS = 8_000
TIMEOUT_S = 300.0
# The SDK retries 429, 5xx and network errors itself. Once is enough: the caller has its own
# retry-then-stronger-model rule on top.
SDK_RETRIES = 1


class AnthropicClient:
    """Claude via the Anthropic SDK, with structured output (`output_config.format`): the API
    constrains the answer to the schema, and it is validated here. The stop reason is checked
    before the answer is read, so a refused or cut-off call is reported as such and keeps the
    cost it ran up. The system prompt is marked for prompt caching. Thinking is left at the
    model's default (adaptive); `effort` sets how much."""

    def __init__(self, model: str, *, sdk_client: Any = None) -> None:
        self.model = model
        self._client = sdk_client if sdk_client is not None else _sdk_client()

    def structured(
        self,
        *,
        system: str,
        user_text: str,
        schema: type[T],
        images: Sequence[tuple[str, bytes]] = (),
        effort: Effort | None = None,
    ) -> LLMResult[T]:
        import anthropic

        content: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
            }
            for media_type, data in images
        ]
        content.append({"type": "text", "text": user_text})
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
        }
        if effort is not None:
            output_config["effort"] = effort

        start = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config=output_config,
            )
        except anthropic.APIStatusError as e:
            # 400, 401, 403, 404, 413, 422: the request itself is wrong; trying again (on this
            # model or another) won't help. 408, 409, 429, 5xx: worth another try.
            kind = "rejected" if 400 <= e.status_code < 500 and e.status_code not in (
                408, 409, 429,
            ) else "api"  # fmt: skip
            raise LLMError(
                kind, str(e)[:300], model=base_model(self.model), latency_ms=_ms(start)
            ) from e
        except anthropic.APIError as e:  # network, timeout (after the SDK's own retry)
            raise LLMError(
                "api", str(e)[:300], model=base_model(self.model), latency_ms=_ms(start)
            ) from e
        latency = _ms(start)

        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        )
        # The API may answer with a dated name ("claude-sonnet-5-5-20261001"); it is the same
        # model, priced the same.
        model = base_model(getattr(response, "model", None) or self.model)
        cost = cost_usd(model, usage)
        billed = {"model": model, "usage": usage, "latency_ms": latency, "cost_usd": cost}
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError("refusal", f"declined ({category or 'no category'})", **billed)
        if response.stop_reason == "max_tokens":
            raise LLMError("truncated", f"stopped at {MAX_TOKENS} tokens", **billed)
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        try:
            value = schema.model_validate_json(text)
        except ValidationError as e:  # not JSON, or values the schema forbids (confidence 1.5)
            raise LLMError("invalid", _short(e), **billed) from e
        return LLMResult(value=value, model=model, usage=usage, latency_ms=latency, cost_usd=cost)


@lru_cache(maxsize=1)
def _sdk_client() -> Any:
    """One SDK client (and connection pool) for the process, shared by every model."""
    import anthropic  # imported lazily so tests don't need the SDK or a key

    return anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=SDK_RETRIES)


@cache
def client_for(model: str) -> AnthropicClient:
    return AnthropicClient(model)


def _short(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:3])


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
