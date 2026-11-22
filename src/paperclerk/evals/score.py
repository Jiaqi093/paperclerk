"""Score saved model answers against labels: the numbers behind every F2 decision.

Order of what matters (labeling guide §4): the red line first (a fabricated value reaching the
library without review must be 0), then the fabrication count, then how many documents are
fully correct, then cost and speed.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field

from paperclerk.evals.data import Label, Prediction
from paperclerk.evals.judge import SCORED_FIELDS, Outcome, judge_field, key_fields
from paperclerk.extract.source_check import normalize_vendor, unsupported_fields

# Confidence bands for the calibration table: lower bound inclusive.
CALIBRATION_BANDS = (0.0, 0.5, 0.7, 0.8, 0.9, 0.95)
MAX_PER_VENDOR = 3  # guide §6: more makes the score swing on one layout


@dataclass(frozen=True)
class DocResult:
    label: Label
    prediction: Prediction | None
    outcomes: dict[str, Outcome]  # empty when the model gave no answer
    fully_correct: bool
    reviewed: bool  # a person would see it before it enters the library
    review_reasons: tuple[str, ...]

    @property
    def failed(self) -> bool:
        return self.prediction is None or self.prediction.output is None

    @property
    def confidence(self) -> float | None:
        return None if self.failed else self.prediction.output.confidence  # type: ignore[union-attr]

    @property
    def fabricated(self) -> list[str]:
        return [f for f, o in self.outcomes.items() if o is Outcome.FABRICATED]

    @property
    def slipped(self) -> bool:
        """The red line: a made-up value went into the library unseen."""
        return bool(self.fabricated) and not self.reviewed

    @property
    def wrong(self) -> bool:
        return not self.fully_correct or bool(self.fabricated)


def judge_document(
    label: Label, prediction: Prediction | None, text: str, threshold: float
) -> DocResult:
    if prediction is None or prediction.output is None:
        # Nothing enters the library; the document needs a person anyway.
        return DocResult(label, prediction, {}, False, True, ("no answer",))
    out = prediction.output
    outcomes = {f: judge_field(f, getattr(label, f), getattr(out, f), text) for f in SCORED_FIELDS}
    fully_correct = outcomes["doc_type"] is Outcome.CORRECT and all(
        outcomes[f] is Outcome.CORRECT for f in key_fields(label)
    )
    reasons = []
    if out.confidence < threshold:
        reasons.append(f"confidence {out.confidence:.2f}")
    missing = unsupported_fields(out, text)
    if missing:
        reasons.append("not in document: " + ", ".join(missing))
    return DocResult(label, prediction, outcomes, fully_correct, bool(reasons), tuple(reasons))


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def labels_fingerprint(labels: list[Label]) -> str:
    """Identifies a label set, so runs are only compared on the same questions."""
    rows = sorted(lab.model_dump_json() for lab in labels)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:12]


@dataclass
class Report:
    docs: list[DocResult]
    threshold: float
    fingerprint: str
    unlabelled_predictions: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.docs)

    @property
    def slipped(self) -> list[DocResult]:
        return [d for d in self.docs if d.slipped]

    @property
    def red_line_ok(self) -> bool:
        return not self.slipped

    @property
    def fabrications(self) -> int:
        return sum(len(d.fabricated) for d in self.docs)

    @property
    def fully_correct(self) -> int:
        return sum(d.fully_correct for d in self.docs)

    @property
    def failed(self) -> int:
        return sum(d.failed for d in self.docs)

    def field_counts(self) -> dict[str, Counter]:
        counts = {f: Counter() for f in SCORED_FIELDS}
        for d in self.docs:
            for f in SCORED_FIELDS:
                # No answer counts against every field: hiding failures would flatter the model.
                counts[f][d.outcomes.get(f, Outcome.MISSED)] += 1
        return counts

    def by_type(self) -> dict[str, tuple[int, int]]:
        """doc type → (fully correct, total), by the label's type."""
        out: dict[str, list[int]] = {}
        for d in self.docs:
            row = out.setdefault(d.label.doc_type.value, [0, 0])
            row[0] += d.fully_correct
            row[1] += 1
        return {k: (v[0], v[1]) for k, v in sorted(out.items())}

    def hard_split(self) -> dict[str, tuple[int, int]]:
        out = {}
        for name, hard in (("normal", False), ("hard", True)):
            group = [d for d in self.docs if d.label.hard is hard]
            if group:
                out[name] = (sum(d.fully_correct for d in group), len(group))
        return out

    def review(self) -> dict[str, int]:
        """Of what went to review, how much was worth a look; of what was wrong, how much
        was caught. Failed calls are left out: no answer means no choice was made."""
        answered = [d for d in self.docs if not d.failed]
        reviewed = [d for d in answered if d.reviewed]
        wrong = [d for d in answered if d.wrong]
        return {
            "reviewed": len(reviewed),
            "reviewed_wrong": sum(d.wrong for d in reviewed),
            "wrong": len(wrong),
            "wrong_caught": sum(d.reviewed for d in wrong),
        }

    def calibration(self) -> list[tuple[float, float, int, int]]:
        """(low, high, documents, fully correct) per confidence band."""
        bounds = [*CALIBRATION_BANDS, 1.0]
        rows = []
        for i, low in enumerate(CALIBRATION_BANDS):
            high = bounds[i + 1]
            last = i == len(CALIBRATION_BANDS) - 1
            group = [
                d
                for d in self.docs
                if d.confidence is not None
                and low <= d.confidence
                and (d.confidence < high or (last and d.confidence <= high))
            ]
            rows.append((low, high, len(group), sum(d.fully_correct for d in group)))
        return rows

    def cost_per_doc(self) -> float | None:
        # A cache hit costs 0 and must count: it is what caching is measured by.
        costs = [
            d.prediction.cost_usd
            for d in self.docs
            if d.prediction and d.prediction.cost_usd is not None
        ]
        return sum(costs) / len(costs) if costs else None

    def latency(self) -> tuple[float | None, float | None]:
        values = [
            d.prediction.latency_ms
            for d in self.docs
            if d.prediction and d.prediction.latency_ms is not None
        ]
        return _percentile(values, 50), _percentile(values, 95)

    def crowded_vendors(self) -> dict[str, int]:
        counts = Counter(normalize_vendor(d.label.vendor) for d in self.docs if d.label.vendor)
        return {v: n for v, n in sorted(counts.items()) if n > MAX_PER_VENDOR}

    def versions(self) -> tuple[list[str], list[str]]:
        preds = [d.prediction for d in self.docs if d.prediction]
        models = sorted({p.model for p in preds if p.model})
        prompts = sorted({p.prompt_version for p in preds if p.prompt_version})
        return models, prompts

    def summary(self) -> dict:
        """One history row: totals only, never file names or document content."""
        models, prompts = self.versions()
        p50, p95 = self.latency()
        cost = self.cost_per_doc()
        return {
            "labels": self.fingerprint,
            "n": self.n,
            "model": ", ".join(models) or None,
            "prompt_version": ", ".join(prompts) or None,
            "threshold": self.threshold,
            "red_line_ok": self.red_line_ok,
            "slipped": len(self.slipped),
            "fabrications": self.fabrications,
            "fully_correct": self.fully_correct,
            "failed": self.failed,
            "cost_per_doc_usd": round(cost, 6) if cost is not None else None,
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
        }


def score(
    labels: list[Label],
    predictions: list[Prediction],
    texts: dict[str, str],
    threshold: float,
) -> Report:
    by_file = {p.file: p for p in predictions}
    labelled = {lab.file for lab in labels}
    docs = [
        judge_document(lab, by_file.get(lab.file), texts[lab.file], threshold) for lab in labels
    ]
    return Report(
        docs=docs,
        threshold=threshold,
        fingerprint=labels_fingerprint(labels),
        unlabelled_predictions=sorted(set(by_file) - labelled),
    )
