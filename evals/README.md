# Evals (F6)

Every prompt or model change is measured here before it is merged.

## Data

A public synthetic set is planned (see [docs/roadmap.md](../docs/roadmap.md#f6--evals-starts-with-f2)). Put real documents in `evals/data/` (git-ignored — they never leave your machine) and label them
in `evals/data/labels.jsonl`, one JSON object per line, using the same fields as
`paperclerk.extract.schemas.Extraction` plus `file`. See `labels.example.jsonl` for the format.

Follow [labeling-guide.md](labeling-guide.md): label before looking at any model output.

Target: ~100 documents covering every `DocType`, including hard cases
(scanned receipts, multi-currency, invoices with tax lines, renewal emails saved as PDF).

## Label

```bash
uv run python evals/label.py
```

Shows each unlabelled document in `evals/data/`, asks its type, then only the fields that type
can have. Enter leaves a field empty. Saved after every document; run again to continue.

## Predict (calls the model)

```bash
uv run python evals/predict.py --labels evals/data/real/labels.jsonl
uv run python evals/predict.py --labels evals/data/sroie/labels.jsonl --model claude-haiku-5-5
```

Runs F2 over the labelled documents and writes `predictions.jsonl` next to the labels. It says
about what it will cost first and asks above `llm.confirm_above_usd`. Model, vision model and
effort default to `config.toml`; override them to compare configs. Reads are cached in
`evals/data/.eval-cache.sqlite`, so running it again with the same settings costs nothing.

## Score

```bash
uv run python evals/run_eval.py --labels evals/data/real/labels.jsonl \
    --predictions evals/data/real/predictions.jsonl [--save]
```

Predictions are the model's saved answers, one JSON object per line:
`{"file", "output": {Extraction fields} | null, "error", "model", "prompt_version", "cost_usd",
"latency_ms"}`. Scoring them is free and repeatable; producing them (F2) is the part that calls
the API.

The scorecard, in order of importance:

1. **Red line**: a fabricated company, amount or date that would reach the library without
   review (confidence above the threshold, and every value found in the document and quoted
   from it: the same check F2 runs). Must be 0;
   the command exits with status 1 otherwise.
2. **Fabrications**: all of them, including those sent to review. Must not go up.
3. **Fully correct**: right type and the key fields of that type. A drop of 1 document is noise.
4. Cost per document and latency.

Then by type, by field (correct / missed / fabricated / wrong), the review queue, a calibration
table, and every mistake with file names (printed only). `--save` appends totals, never file
names, to `results.md`; runs are compared only on the same label set.
