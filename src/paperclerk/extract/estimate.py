"""F2 step 5: about what reading documents will cost, before anything is spent.

On the high side on purpose: the whole prompt is priced as if it weren't cached, every page as
sent in full, the answer with room for thinking, and everything once more by half for retries
and second reads by a stronger model. A guide, not a promise: one document can still cost up to
three reads.
"""

from __future__ import annotations

import math

from paperclerk.extract.inputs import CHARS_PER_TOKEN, IMAGE_LONG_EDGE, IMAGE_PATCH, _text_tokens
from paperclerk.extract.prompt import SYSTEM_PROMPT
from paperclerk.ingest.parsers.base import ParsedDocument
from paperclerk.llm.base import Usage
from paperclerk.llm.pricing import cost_usd

# An A4 page rendered at IMAGE_LONG_EDGE: about 1414 x 2000 pixels.
IMAGE_TOKENS_PER_PAGE = math.ceil(IMAGE_LONG_EDGE / 1.414 / IMAGE_PATCH) * math.ceil(
    IMAGE_LONG_EDGE / IMAGE_PATCH
)
OUTPUT_TOKENS = 3_000  # the answer (~500) plus thinking
MARGIN = 1.5  # retries and second reads
_PROMPT_TOKENS = math.ceil(len(SYSTEM_PROMPT) / CHARS_PER_TOKEN) + 1_000  # + the schema


def model_for(doc: ParsedDocument, model: str, vision_model: str | None) -> str:
    return vision_model if doc.needs_vision and vision_model else model


def document_cost(doc: ParsedDocument, *, model: str, vision_model: str | None) -> float:
    """USD to read one document once. A model with no known price counts as the most
    expensive one we know, so an estimate never comes out as zero."""
    tokens = _PROMPT_TOKENS
    for page in doc.pages:
        tokens += _text_tokens(page.text)
        if page.needs_vision:
            tokens += IMAGE_TOKENS_PER_PAGE
    usage = Usage(input_tokens=tokens, output_tokens=OUTPUT_TOKENS)
    cost = cost_usd(model_for(doc, model, vision_model), usage)
    if cost is None:
        cost = cost_usd("claude-opus-5-5", usage)
    return (cost or 0.0) * MARGIN
