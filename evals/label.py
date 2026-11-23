"""Label documents in evals/data/ one by one, following evals/labeling-guide.md.

    uv run python evals/label.py

Shows each unlabelled document's text, asks its type, then only the fields that type can have.
Enter leaves a field empty (the document doesn't show it). Progress is saved after every
document; run it again to continue. Model answers are never shown here: label first.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from paperclerk.evals.data import Label, load_labels
from paperclerk.evals.labeling import (
    FIELDS_BY_TYPE,
    HINTS,
    InvalidAnswer,
    parse_answer,
    parse_doc_type,
    unlabelled_files,
)
from paperclerk.evals.score import MAX_PER_VENDOR
from paperclerk.extract.schemas import DocType
from paperclerk.extract.source_check import normalize_vendor
from paperclerk.ingest.parsers import ParseError, parser_for

PREVIEW_CHARS = 2500


class Skip(Exception):
    pass


class Quit(Exception):
    pass


def ask(prompt: str) -> str:
    try:
        answer = input(prompt)
    except EOFError:
        raise Quit from None
    if answer.strip() == "q":
        raise Quit
    if answer.strip() == "s":
        raise Skip
    return answer


def show(path: Path, rel: str, index: int, total: int) -> None:
    print("\n" + "=" * 78)
    print(f"[{index}/{total}] {rel}")
    parser = parser_for(path)
    try:
        doc = parser.parse(path.read_bytes(), path) if parser else None
    except ParseError as e:
        print(f"  (can't parse: {e}; open the file to label it)")
        return
    if doc is None:
        return
    scanned = [i + 1 for i, p in enumerate(doc.pages) if p.needs_vision]
    if scanned:
        print(f"  scanned page(s) {scanned}: their text isn't shown. Type o to open the file.")
    text = doc.text.strip()
    print("-" * 78)
    print(text[:PREVIEW_CHARS] if text else "(no text)")
    if len(text) > PREVIEW_CHARS:
        print(f"... ({len(text) - PREVIEW_CHARS} more characters; o opens the file)")
    print("-" * 78)


def ask_type(path: Path) -> DocType:
    choices = "  ".join(f"{i}={t.value}" for i, t in enumerate(DocType, start=1))
    while True:
        answer = ask(f"type ({choices}; o=open, s=skip, q=quit): ")
        if answer.strip() == "o":
            subprocess.run(["open", str(path)], check=False)
            continue
        try:
            return parse_doc_type(answer)
        except InvalidAnswer as e:
            print(f"  {e}")


def ask_field(field: str) -> object:
    while True:
        try:
            return parse_answer(field, ask(f"{field} ({HINTS[field]}): "))
        except InvalidAnswer as e:
            print(f"  {e}")


def label_one(path: Path, rel: str, vendors: Counter) -> Label:
    while True:
        doc_type = ask_type(path)
        values: dict[str, object] = {"file": rel, "doc_type": doc_type.value}
        for field in FIELDS_BY_TYPE[doc_type]:
            values[field] = ask_field(field)
            if field == "vendor" and values[field]:
                seen = vendors[normalize_vendor(str(values[field]))]
                if seen >= MAX_PER_VENDOR:
                    print(
                        f"  already {seen} documents from this company (guide: at most"
                        f" {MAX_PER_VENDOR}); s skips this one"
                    )
        values["hard"] = ask("hard case? (y/N): ").strip().lower() in ("y", "yes")
        values["note"] = ask("note (optional): ").strip() or None
        label = Label.model_validate(values)
        print("  " + label.model_dump_json(exclude_none=True, exclude_defaults=True))
        if ask("Enter to save, r to redo: ").strip().lower() != "r":
            return label


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--labels", type=Path, default=Path(__file__).parent / "data" / "labels.jsonl")
    args = ap.parse_args()
    root = args.labels.parent
    labels = load_labels(args.labels) if args.labels.exists() else []
    todo = unlabelled_files(root, (lab.file for lab in labels))
    if not todo:
        sys.exit(f"nothing to label: put documents in {root}/")
    vendors = Counter(normalize_vendor(lab.vendor) for lab in labels if lab.vendor)
    print(f"{len(labels)} labelled, {len(todo)} to go. Rules: evals/labeling-guide.md")
    done = 0
    for i, rel in enumerate(todo, start=1):
        path = root / rel
        show(path, rel, i, len(todo))
        try:
            label = label_one(path, rel, vendors)
        except Skip:
            continue
        except Quit:
            break
        with args.labels.open("a", encoding="utf-8") as f:
            f.write(json.dumps(label.model_dump(mode="json", exclude_none=True)) + "\n")
        if label.vendor:
            vendors[normalize_vendor(label.vendor)] += 1
        done += 1
    print(f"\nsaved {done} label(s) to {args.labels}")


if __name__ == "__main__":
    main()
