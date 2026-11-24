# Eval results

Written by `evals/run_eval.py --save`. Totals only: no file names or document content.
Compare rows only when the Labels fingerprint matches (same questions).

| Date | Set | Labels | Docs | Model | Prompt | Red line | Fabrications | Fully correct | Sent to review | Cost/doc ($) | p50 (ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-25 | real | dcf0a1c89bd1 | 19 | claude-opus-5-5, claude-sonnet-5-5 | classify-3d5719e452 | pass | 0 | 19/19 | 2/19 | 0.010393 | 2633.0 |
| 2026-11-25 | sroie | 6114b9661a38 | 60 | claude-opus-5-5 | classify-3d5719e452 | pass | 11 | 48/60 | 60/60 | 0.014343 | 3718.0 |
