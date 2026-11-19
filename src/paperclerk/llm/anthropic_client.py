from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AnthropicClient:
    """Claude via the Anthropic SDK. Structured output is implemented with a single forced tool."""

    def __init__(self, model: str) -> None:
        import anthropic  # imported lazily so tests don't need the SDK or a key

        self.model = model
        self._client = anthropic.Anthropic()

    def structured(
        self,
        *,
        system: str,
        user_text: str,
        schema: type[T],
        images: list[bytes] | None = None,
    ) -> T:
        # TODO(F2): tool = {name, input_schema: schema.model_json_schema()},
        # tool_choice forced, validate with schema.model_validate, retry once on ValidationError.
        raise NotImplementedError("Anthropic client not implemented yet (F2)")
