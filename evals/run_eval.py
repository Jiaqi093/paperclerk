"""Score the F2 pipeline against hand-labelled documents. Implemented in week 2 (F6)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from paperclerk.extract.schemas import Extraction

FIELDS = ["doc_type", "vendor", "amount", "currency", "issue_date", "due_date"]


def load_labels(path: Path) -> list[tuple[str, Extraction]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            out.append((obj.pop("file"), Extraction.model_validate(obj)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", type=Path, default=Path("evals/data/labels.jsonl"))
    args = ap.parse_args()
    labels = load_labels(args.labels)
    print(f"loaded {len(labels)} labelled documents")
    # TODO(week 2): run classify() on each file, compare FIELDS, print a table, append results.md
    raise SystemExit("scoring not implemented yet")


if __name__ == "__main__":
    main()
