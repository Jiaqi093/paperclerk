from pathlib import Path

import pytest
from pydantic import ValidationError

from paperclerk.extract.schemas import DocType, Extraction

EXAMPLES = Path(__file__).parent.parent / "evals" / "labels.example.jsonl"


def test_example_labels_match_schema():
    import json

    for line in EXAMPLES.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        obj.pop("file")
        Extraction.model_validate(obj)


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        Extraction(doc_type=DocType.INVOICE, confidence=1.5)
