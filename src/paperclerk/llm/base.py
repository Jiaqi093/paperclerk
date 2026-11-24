"""A deliberately thin model interface. Swapping providers should only touch this package."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Effort = Literal["low", "medium", "high", "xhigh", "max"]
# rejected: the API refused the request itself (400, 401, 404, ...): retrying won't help.
ErrorKind = Literal["refusal", "truncated", "invalid", "api", "rejected"]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    value: T
    model: str
    usage: Usage
    latency_ms: int
    cost_usd: float | None  # None when the model's price isn't known


class LLMError(Exception):
    """The call gave no usable answer. A call that ran still cost money: usage and cost say how
    much, when the API reported it."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        model: str,
        usage: Usage | None = None,
        latency_ms: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.model = model
        self.usage = usage
        self.latency_ms = latency_ms
        self.cost_usd = cost_usd


class LLMClient(Protocol):
    model: str

    def structured(
        self,
        *,
        system: str,
        user_text: str,
        schema: type[T],
        images: Sequence[tuple[str, bytes]] = (),
        effort: Effort | None = None,
    ) -> LLMResult[T]:
        """One call. `images` are (media type, bytes), shown before the text. Returns a validated
        instance of `schema`, or raises LLMError."""
        ...
