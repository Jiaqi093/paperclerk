"""Score saved model answers against the labels and print the scorecard.

    uv run python evals/run_eval.py --predictions evals/data/predictions.jsonl
    uv run python evals/run_eval.py --predictions ... --save    # also append to results.md

Predictions come from running F2 over the labelled documents (not written yet). Rules:
evals/labeling-guide.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from paperclerk.config import load_settings
from paperclerk.evals.data import DataError, load_labels, load_predictions, read_texts
from paperclerk.evals.report import load_history, previous_run, render, save_run
from paperclerk.evals.score import score

EVALS = Path(__file__).parent
MAX_MISSING = 0.1  # share of labelled documents allowed to have no prediction at all


def _set_name(args: argparse.Namespace) -> str | None:
    """The label set's name for the committed results: --set, or the folder under evals/data/
    the labels are in. Never another folder's name (it could be personal), never free text
    that could break the table."""
    name = args.set
    if name is None:
        data = (EVALS / "data").resolve()
        parent = args.labels.resolve().parent
        name = parent.name if parent.parent == data else None
    if name is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        sys.exit(f"--set must be a short name (letters, digits, - or _), not {name!r}")
    return name


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--labels", type=Path, default=EVALS / "data" / "labels.jsonl")
    ap.add_argument("--predictions", type=Path, required=True)
    ap.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="review below this confidence (default: llm.review_threshold in config.toml)",
    )
    ap.add_argument("--config", type=Path, default=Path("config.toml"))
    ap.add_argument("--save", action="store_true", help="append totals to evals/results.md")
    ap.add_argument("--set", help="name of the label set in results.md (default: its folder)")
    args = ap.parse_args()

    threshold = args.threshold
    if threshold is None:
        threshold = load_settings(args.config).llm.review_threshold
    try:
        labels = load_labels(args.labels)
        predictions = load_predictions(args.predictions)
        texts = read_texts(labels, args.labels.parent)
    except (DataError, FileNotFoundError) as e:
        sys.exit(str(e))
    if not labels:
        sys.exit(f"no labels in {args.labels}")

    # Mismatched paths would turn every document into "no answer", which can't fabricate and
    # would pass the red line. Stop instead of printing a misleading pass.
    answered = {p.file for p in predictions}
    missing = [lab.file for lab in labels if lab.file not in answered]
    if len(missing) > MAX_MISSING * len(labels):
        sys.exit(
            f"{len(missing)} of {len(labels)} labelled documents have no prediction"
            f" (e.g. {missing[0]}); paths must match the labels' file names"
        )

    report = score(labels, predictions, texts, threshold)
    history_path = EVALS / "results.jsonl"
    previous = previous_run(load_history(history_path), report.fingerprint)
    print(render(report, previous))
    if args.save:
        save_run(report, history_path, EVALS / "results.md", data_set=_set_name(args))
        print(f"\nsaved to {EVALS / 'results.md'}")
    if not report.red_line_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
