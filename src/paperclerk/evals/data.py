"""Labels (the correct answers) and predictions (the model's saved answers), read from JSONL.

Predictions are kept apart from scoring so a change to the scoring rules re-scores saved answers
without calling the model again.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from paperclerk.extract.schemas import DocType, Extraction, Period


class Label(BaseModel):
    # A misspelt key ("due_dat") must fail loudly, not silently become an empty field.
    model_config = ConfigDict(extra="forbid")

    file: str = Field(..., description="Path relative to the labels file's folder")
    doc_type: DocType
    vendor: str | None = None
    amount: float | None = None
    currency: str | None = None
    issue_date: date | None = None
    due_date: date | None = None
    is_recurring: bool | None = None
    period: Period | None = None
    hard: bool = False
    note: str | None = None
    # Accepted (labels.example.jsonl has it) but unused: a label is always the right answer.
    confidence: float | None = None

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a three-letter code such as AUD")
        return v

    @field_validator("vendor", "note")
    @classmethod
    def _blank_is_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    output: Extraction | None = None  # None when the call failed
    error: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None


class DataError(ValueError):
    """A labels or predictions file can't be used; the message says which line and why."""


def _read_jsonl(path: Path, model: type[BaseModel]) -> list:
    rows, seen = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = model.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as e:
            raise DataError(f"{path.name} line {n}: {e}") from e
        if row.file in seen:
            raise DataError(f"{path.name} line {n}: {row.file} appears twice")
        seen.add(row.file)
        rows.append(row)
    return rows


def load_labels(path: Path) -> list[Label]:
    return _read_jsonl(path, Label)


def load_predictions(path: Path) -> list[Prediction]:
    return _read_jsonl(path, Prediction)


def read_texts(labels: list[Label], root: Path) -> dict[str, str]:
    """Each labelled document's text, parsed with the same parsers as a scan (F1).

    The source check looks for the model's values in this text. Scanned pages have none, so
    values on them can't be found: the strict rule for scans.
    """
    from paperclerk.ingest.parsers import ParseError, parser_for

    texts, problems = {}, []
    for label in labels:
        path = root / label.file
        parser = parser_for(path)
        if not path.is_file():
            problems.append(f"{label.file}: file not found")
        elif parser is None:
            problems.append(f"{label.file}: unsupported file type")
        else:
            try:
                texts[label.file] = parser.parse(path.read_bytes(), path).text
            except ParseError as e:
                problems.append(f"{label.file}: {e}")
    if problems:
        raise DataError("can't read labelled documents:\n  " + "\n  ".join(problems))
    return texts
