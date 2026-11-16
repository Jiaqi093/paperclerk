"""F2 — classify a parsed document and extract structured fields."""

from __future__ import annotations

from paperclerk.extract.schemas import Extraction
from paperclerk.ingest.parsers.base import ParsedDocument
from paperclerk.llm.base import LLMClient


def classify(doc: ParsedDocument, llm: LLMClient) -> Extraction:
    # TODO(week 1): load prompts/classify.md, call llm.structured(..., schema=Extraction),
    # send page images instead of text when doc.needs_vision.
    raise NotImplementedError("Classification lands in week 1 (F2)")
