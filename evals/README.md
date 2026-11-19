# Evals (F6)

Every prompt or model change is measured here before it is merged.

## Data

A public synthetic set is planned (see [docs/roadmap.md](../docs/roadmap.md#f6--evals-starts-with-f2)). Put real documents in `evals/data/` (git-ignored — they never leave your machine) and label them
in `evals/data/labels.jsonl`, one JSON object per line, using the same fields as
`paperclerk.extract.schemas.Extraction` plus `file`. See `labels.example.jsonl` for the format.

Target: ~100 documents covering every `DocType`, including hard cases
(scanned receipts, multi-currency, invoices with tax lines, renewal emails saved as PDF).

## Metrics

| Metric | What it measures |
|---|---|
| `doc_type` accuracy | classification |
| per-field match (`vendor`, `amount`, `currency`, `issue_date`, `due_date`): amount within 0.01, vendor names normalized, dates compared as dates | extraction |
| review-queue precision | of items flagged low-confidence, how many were actually wrong |
| duplicate detection precision / recall | same invoice from email + disk merged correctly |
| confidence calibration | whether stated confidence matches accuracy; sets `review_threshold` |
| cost and latency per document | recorded with every run |

## Run

```bash
uv run python evals/run_eval.py --labels evals/data/labels.jsonl
```

Results are appended to `evals/results.md` so the README can show progress over time.
