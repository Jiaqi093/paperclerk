"""The system prompt for classification and extraction, and its version.

The rules part is sections 1-5 of evals/labeling-guide.md, copied word for word into
prompts/rules.md (a test fails if they drift), so the model answers by the same rules the labels
are written by. Change a rule in the guide, then copy it here.

PROMPT_VERSION is a hash of everything the model sees besides the document itself, so any edit
gets a new version without anyone having to remember to bump it: evals compare runs by it, and
cached answers are keyed on it.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources

from paperclerk.extract.inputs import MARKUP_VERSION
from paperclerk.extract.schemas import ModelAnswer

_PROMPTS = resources.files("paperclerk.extract") / "prompts"


def _load() -> str:
    template = (_PROMPTS / "classify.md").read_text(encoding="utf-8")
    rules = (_PROMPTS / "rules.md").read_text(encoding="utf-8").strip()
    return template.replace("{rules}", rules)


def _version(system_prompt: str) -> str:
    # The prompt, the answer schema (its fields and descriptions go to the API too) and the
    # markup the document is wrapped in.
    schema = json.dumps(ModelAnswer.model_json_schema(), sort_keys=True)
    seen = "\n".join([system_prompt, schema, f"markup {MARKUP_VERSION}"])
    return "classify-" + hashlib.sha256(seen.encode()).hexdigest()[:10]


SYSTEM_PROMPT = _load()
PROMPT_VERSION = _version(SYSTEM_PROMPT)
