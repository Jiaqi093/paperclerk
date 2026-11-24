"""Print a scorecard and keep the run history (totals only, safe to commit)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from paperclerk.evals.judge import Outcome
from paperclerk.evals.score import MAX_PER_VENDOR, DocResult, Report
from paperclerk.extract.source_check import LITERAL_FIELDS

# Differences this small are noise at the current set size (30 documents: 1 document ≈ 3%).
FLAT_DOCS = 1


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.0%})" if whole else "0/0"


def _value(v: Any) -> str:
    if v is None:
        return "—"
    return getattr(v, "value", None) or str(v)


def verdict(current: dict, previous: dict | None) -> str:
    """Can this change stay? Red line, then fabrications, then fully correct (guide §4)."""
    if not current["red_line_ok"]:
        return "REJECT: a fabricated value reached the library unreviewed"
    if previous is None:
        return "first run on these labels"
    if current["fabrications"] > previous["fabrications"]:
        return "REJECT: more fabrications than last run"
    if current.get("failed", 0) > previous.get("failed", 0):
        # Failed calls can't fabricate, so more failures could hide fabrications.
        return "REJECT: more failed calls than last run"
    if current["fully_correct"] < previous["fully_correct"] - FLAT_DOCS:
        return "REJECT: fewer fully correct documents"
    return "OK: red line held, fabrications not up, accuracy held"


def _delta(current: dict, previous: dict | None, key: str) -> str:
    if previous is None or previous.get(key) is None or current.get(key) is None:
        return ""
    d = current[key] - previous[key]
    if key == "fully_correct" and abs(d) <= FLAT_DOCS:
        return f"  ({d:+d} vs last run: flat)"
    if isinstance(d, float):
        return f"  ({d:+.4f} vs last run)"
    return f"  ({d:+d} vs last run)"


def _problem_line(d: DocResult) -> list[str]:
    head = d.label.file
    tags = []
    if d.slipped:
        tags.append("SLIPPED INTO LIBRARY")
    if d.label.hard:
        tags.append("hard")
    if d.confidence is not None:
        tags.append(f"confidence {d.confidence:.2f}")
    if d.reviewed:
        tags.append("to review: " + "; ".join(d.review_reasons))
    lines = [f"  {head}  [{', '.join(tags)}]"]
    if d.failed:
        error = d.prediction.error if d.prediction else "no prediction"
        lines.append(f"      no answer: {error or 'unknown error'}")
        return lines
    out = d.prediction.output  # type: ignore[union-attr]
    for f, o in d.outcomes.items():
        if o is not Outcome.CORRECT:
            # "Wrong" on a company, amount or date rests on the source check finding the value
            # in the document; a false find would hide a fabrication, so ask for a look.
            check = (
                "  (check it is on the document)"
                if f in LITERAL_FIELDS and o is Outcome.WRONG
                else ""
            )
            lines.append(
                f"      {f}: {o.value}  label {_value(getattr(d.label, f))}"
                f"  model {_value(getattr(out, f))}{check}"
            )
    return lines


def render(report: Report, previous: dict | None = None) -> str:
    s = report.summary()
    lines = []
    if report.red_line_ok:
        lines.append("Red line: PASSED (no fabricated value reached the library unreviewed)")
    else:
        lines.append(f"Red line: FAILED ({len(report.slipped)} document(s), listed below)")
    lines += [
        f"Verdict: {verdict(s, previous)}",
        "",
        f"Fully correct:  {_pct(report.fully_correct, report.n)}"
        + _delta(s, previous, "fully_correct"),
        f"Fabrications:   {report.fabrications}" + _delta(s, previous, "fabrications"),
        f"Failed calls:   {report.failed}",
    ]
    cost = report.cost_per_doc()
    p50, p95 = report.latency()
    if cost is not None:
        lines.append(f"Cost per doc:   ${cost:.4f}" + _delta(s, previous, "cost_per_doc_usd"))
    if p50 is not None:
        lines.append(f"Latency:        p50 {p50 / 1000:.1f} s, p95 {p95 / 1000:.1f} s")
    models, prompts = report.versions()
    lines.append(f"Model / prompt: {', '.join(models) or '—'} / {', '.join(prompts) or '—'}")

    lines += ["", "By type (label's type): fully correct"]
    for t, (ok, total) in report.by_type().items():
        lines.append(f"  {t:<13} {_pct(ok, total)}")
    split = report.hard_split()
    if "hard" in split:
        lines.append("  " + "   ".join(f"{k}: {_pct(*v)}" for k, v in split.items()))

    lines += ["", f"By field        {'correct':>8} {'missed':>7} {'fabric.':>8} {'wrong':>6}"]
    for f, c in report.field_counts().items():
        lines.append(
            f"  {f:<13} {c[Outcome.CORRECT]:>8} {c[Outcome.MISSED]:>7}"
            f" {c[Outcome.FABRICATED]:>8} {c[Outcome.WRONG]:>6}"
        )

    r = report.review()
    lines += [
        "",
        f"Review queue (confidence below {report.threshold:.2f}, or a value not in the document"
        " or not quoted from it: the same check F2 runs)",
        f"  sent to review: {r['reviewed']}, of which wrong: {r['reviewed_wrong']}",
        f"  wrong: {r['wrong']}, of which caught: {r['wrong_caught']},"
        f" went into the library: {r['wrong'] - r['wrong_caught']}",
        "",
        "Calibration: confidence band → fully correct",
    ]
    for low, high, total, ok in report.calibration():
        if total:
            lines.append(f"  {low:.2f}–{high:.2f}  {_pct(ok, total)}")

    problems = [d for d in report.docs if d.wrong or d.failed]
    if problems:
        lines += ["", "Documents with mistakes (names stay on this machine)"]
        for d in sorted(problems, key=lambda d: (not d.slipped, d.label.file)):
            lines += _problem_line(d)

    warnings = []
    crowded = report.crowded_vendors()
    if crowded:
        listed = ", ".join(f"{v} ({n})" for v, n in crowded.items())
        warnings.append(f"more than {MAX_PER_VENDOR} documents from one company: {listed}")
    if report.unlabelled_predictions:
        n = len(report.unlabelled_predictions)
        warnings.append(f"{n} prediction(s) without a label, ignored")
    if warnings:
        lines += ["", "Warnings"] + [f"  {w}" for w in warnings]
    return "\n".join(lines)


# --- history ---------------------------------------------------------------------------


def load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def previous_run(history: list[dict], fingerprint: str) -> dict | None:
    """The last run on the same labels: runs on different questions can't be compared."""
    return next((h for h in reversed(history) if h.get("labels") == fingerprint), None)


_COLUMNS = [
    ("date", "Date"),
    ("labels", "Labels"),
    ("n", "Docs"),
    ("model", "Model"),
    ("prompt_version", "Prompt"),
    ("red_line_ok", "Red line"),
    ("fabrications", "Fabrications"),
    ("fully_correct", "Fully correct"),
    ("cost_per_doc_usd", "Cost/doc ($)"),
    ("latency_p50_ms", "p50 (ms)"),
]


def _cell(row: dict, key: str) -> str:
    v = row.get(key)
    if key == "red_line_ok":
        return "pass" if v else "**FAIL**"
    if key == "fully_correct" and row.get("n"):
        return f"{v}/{row['n']}"
    return "—" if v is None else str(v)


def save_run(report: Report, jsonl: Path, markdown: Path, today: date | None = None) -> dict:
    row = {"date": (today or date.today()).isoformat(), **report.summary()}
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    history = load_history(jsonl)
    lines = [
        "# Eval results",
        "",
        "Written by `evals/run_eval.py --save`. Totals only: no file names or document content.",
        "Compare rows only when the Labels fingerprint matches (same questions).",
        "",
        "| " + " | ".join(title for _, title in _COLUMNS) + " |",
        "|" + "---|" * len(_COLUMNS),
    ]
    lines += ["| " + " | ".join(_cell(h, k) for k, _ in _COLUMNS) + " |" for h in history]
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return row
