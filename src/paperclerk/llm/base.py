"""A deliberately thin model interface. Swapping providers should only touch this package."""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    model: str

    def structured(
        self,
        *,
        system: str,
        user_text: str,
        schema: type[T],
        images: list[bytes] | None = None,
    ) -> T:
        """Return an instance of `schema`, validated. Raise on invalid output after retries."""
        ...
