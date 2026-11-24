"""What a call cost, from the token counts the API reports.

Prices in USD per million tokens, Claude API list prices (November 2026). Cache writes are
priced at 1.25x input (5-minute cache); cache reads as listed. A model not in the table gets
no cost (None) rather than a wrong one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from paperclerk.llm.base import Usage


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    # Haiku 5.5 costs more above 100K prompt tokens.
    long_from: int | None = None
    long_input: float | None = None
    long_output: float | None = None


PRICES: dict[str, Price] = {
    # Haiku's cache-read price isn't on the list we have: assumed the usual 0.1x input.
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 100_000, 0.50, 2.50),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.10),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
}


_DATED = re.compile(r"-\d{8}$")


def base_model(model: str) -> str:
    """ "claude-sonnet-5-5-20261001" → "claude-sonnet-5-5": a dated name is the same model."""
    return _DATED.sub("", model)


def cost_usd(model: str, usage: Usage) -> float | None:
    price = PRICES.get(base_model(model))
    if price is None:
        return None
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    inp, out, read = price.input, price.output, price.cache_read
    if price.long_from is not None and prompt > price.long_from:
        factor = price.long_input / price.input
        inp, out, read = price.long_input, price.long_output, read * factor
    total = (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_read_tokens * read
        + usage.cache_write_tokens * inp * 1.25
    )
    return total / 1_000_000
