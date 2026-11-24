# Roadmap

The working plan, stage by stage. Tick items as they land. The README keeps a one-line-per-week
summary; design decisions and their reasons go in [architecture.md](architecture.md).

Each stage lists what it must deliver and a **done when** check. Items marked _(new)_ were added
in the November 2026 plan review: the original 4-week plan covered the product features, and
these add the engineering around the model (evals, cost, observability, safety, retrieval).

## Timeline (6 weeks)

| Week | Focus |
|---|---|
| 1 | Finish F1 · F2 basics (LLM client, call log, extraction) · F6 v1 (~30 labels + synthetic set) |
| 2 | Finish F2 (review UI, background jobs, model routing, caching) · first F6 numbers |
| 3 | F4 organize agent (hand-written + LangGraph, safety tests) |
| 4 | F3 Gmail sync (`document_sources`, injection defense, dedupe) |
| 5 | F5 retrieval, Q&A, bills dashboard, streaming |
| 6 | MCP server, Docker, hosted demo, README numbers, demo video |
| later | F7 distillation (stretch) |

F6 starts alongside F2 rather than after it: prompt, model and cost decisions in F2 should be
made from numbers.

---

## F1 — Local file ingest

Done in PR #2: incremental scan, content-hash identity, `document_paths`, error isolation,
schema migrations.

- [x] PDF parser (pypdfium2, not PyMuPDF: AGPL): text per page; a page with under
      `MIN_CHARS_PER_PAGE` (30) chars and a picture or outlined text on it, or mostly covered by
      images with little text, gets `needs_vision`. Failure reasons ("password protected") stored in `parse_error`.
- [x] DOCX parser (python-docx, reading the XML): paragraphs, tables as Markdown, headers and
      footers, text boxes; tracked deletions and comments skipped; one page. A body with an
      image and little text gets `needs_vision`. Old `.doc` stays unsupported.
- [x] Store per-page text and `needs_vision` _(new — parsers return them, the database drops
      them)_: a `document_pages (document_id, page_no, text)` table and a `needs_vision` column.
      F2 needs the flag to route scans to vision; F5 needs pages for chunking and page citations.
- [x] `parser_version`: each parser has a `version`; bumping it re-parses every file of that
      type once. Same pages → only the version is updated; changed pages are replaced (F2:
      mark extractions stale then, never overwrite `human_verified`). **Bump the version
      whenever a parser change could alter its output.**
- [x] Cloud-only files (iCloud "Optimize Mac Storage", Dropbox, OneDrive: macOS
      `SF_DATALESS`) are never read, so scans don't download them; reported in
      `cloud_only`, known ones keep their paths. Not yet tested on a real sync folder.
- [x] Small fixes from the PR #2 review: `paths_under` as a range query; web shows removed,
      cloud-only and failure reasons, plus a status column; migrate's `ROLLBACK` guarded; the
      scan takes the database lock per file, so the API stays responsive during a scan.
      (Unreadable folders are still reported on every scan, on purpose.)

Real-folder check (2026-11-20, 40 real bills: energy, water, internet, insurance, tickets):
all parsed, no failures, rescan does no work. It found two bugs, fixed in PdfParser v2: a nearly
empty last page was flagged for vision, and PDFium's U+FFFE hyphen marker leaked into text.
A second check on all of ~/Documents (114 files: 94 PDF, 3 DOCX, 4 JPG, 13 Markdown; only
statistics looked at, no file names or text): all parsed, no failures; 3 exact copies merged.
6 scanned PDFs and 1 mixed one were flagged; one PDF had its text drawn as vector outlines
with no text layer, and only 1 of its 6 pages was flagged. PdfParser v3 finds outlines by
rendering the page small and counting lines of ink (all 6 flagged now). DOCX and images parsed,
but their text quality wasn't inspected (privacy).

**Done when:** a real folder mixing text PDFs, scanned PDFs, DOCX and images scans cleanly; every
document has text or `needs_vision`; encrypted and corrupt files have tests.

## F6 — Evals (starts with F2)

Scoring and labeling tool done (`evals/run_eval.py`, `evals/label.py`). First real numbers on
2026-11-25 (F2 core): 19/19 real documents fully correct, 48/60 SROIE scans, red line held on
both; totals in `evals/results.md` (with the label set's name) and the README.

- [x] Labeling guide ([evals/labeling-guide.md](../evals/labeling-guide.md)): one correct answer
      per document, type tie-break order, what "amount" means per type, key fields per type, at
      most 3 documents per company, labels changed only when they break a rule
- [x] Fabrication red line: a fabricated value reaching the library without review must be 0
      (one case blocks the change, the scorer exits non-zero); all fabrications are counted and
      must not increase. Fabrication concerns company, amount and dates only; on scans a wrong
      value that can't be found in the text counts as fabricated (strict).
- [x] Fully correct = right type + the key fields of that type (invoice: company, amount, due
      date; receipt: company, amount, payment date; subscription: company, amount, period,
      renewal date; credit card statement: company, amount, due date; others: type only)
- [x] Field matching rules _(new)_: amount within 0.01; vendor names normalized
      ("Origin Energy Pty Ltd" = "Origin Energy"); dates compared as dates
- [x] Scorecard: verdict vs the last run on the same labels (red line → fabrications → fully
      correct, 1 document = flat), by type, by field (correct / missed / fabricated / wrong),
      review queue, calibration table, every mistake listed; totals appended to
      `evals/results.md` with `--save`
- [x] Cost and latency recorded with every run _(new)_, when predictions carry them
- [x] Labeling tool: shows the text, asks the type, then only that type's fields; warns past 3
      documents per company; never shows model answers
- [ ] ~30 labelled real documents, then ~100 covering every `DocType`, with bait documents
      (promotions, quotes, emails with dates). 19 labelled so far (2026-11-23: bills, receipts,
      insurance payment history); bait and scans to come from Gmail (F3)
- [ ] Public sets scored separately _(new)_: 60 SROIE receipts sampled (scans, Malaysia;
      company, date and total annotated). DocILE needs an access token. Still to build:
      fields a set doesn't annotate go unscored, one scorecard per set
- [x] Predictions: `evals/predict.py` runs F2 over the labelled documents into
      `predictions.jsonl` (model, vision model and effort can be overridden to compare
      configs; reads are cached in `evals/data/.eval-cache.sqlite`, so re-scoring is free)
- [ ] Synthetic dataset _(new)_: generated invoices, receipts and renewal emails (PDF and image)
      that can be published, so results are reproducible. Real documents stay a private
      supplement in `evals/data/`.
- [ ] Confidence calibration _(new)_: set `review_threshold` (now 0.8) from the calibration
      table: the lowest review load with at most 5% of wrong documents reaching the library
- [ ] CI _(new)_: scoring code is tested on every PR; still to do: score cached outputs of the
      synthetic set. Full runs that call the API are triggered manually.
- [ ] Dedupe precision/recall (F3)
- [ ] Later suites _(new)_: prompt-injection set (F3/F4), retrieval recall@k and MRR (F5),
      Q&A answers graded by an LLM judge that is checked against human grades (F5)

**Done when:** one command prints accuracy, cost per document and latency; the README shows the
first real numbers.

## F2 — Classification and extraction

- [x] `AnthropicClient.structured()`: structured output (`output_config.format`; current models
      reject forced tool calls), stop reason checked before the answer is read, usage and cost
      kept for every call, refused and cut-off ones included. Model and effort set in config.
- [x] Failure rule: retry once on the same model, then once on the next stronger one (Haiku →
      Sonnet → Opus); still failing → failed call, sent to review. Requests the API rejects as
      malformed aren't retried.
- [x] Prompt = sections 1-5 of the labeling guide word for word (a test guards drift) plus how
      to read the input and answer; the model quotes the document for each company, amount and
      date (`evidence`). `prompt_version` hashes prompt, answer schema and markup.
- [x] Model input (`extract/inputs.py`): pages with text go as text only; pages flagged
      `needs_vision` go as JPEG images (PDF pages rendered with pypdfium2, photos turned upright,
      DOCX scans as their embedded pictures), long edge 2000 px. The whole document is sent,
      never cut for length; only what one request can carry (context window, 600 images,
      128 MB) can leave pages out, and then the document goes to review. The file name is sent as a hint only (no
      folders); document text is wrapped as data and can't close its own tags.
- [x] Source and evidence check (`extract/verify.py`): every company, amount and date must be
      written in the document and quoted from it (the quote really in the document, showing
      that value); any problem sends the document to review. F6 runs the same check, so its
      red line is the real one. A failed check gets one more read by the next stronger model,
      kept if it is no worse (a value left out counts against it); values the two reads
      disagree on go to review whichever is kept. Values from scanned pages can't be checked
      yet: those documents go to review (next: on-device text recognition).
- [x] Documents with scanned pages are read by `llm.vision_model` (default Opus 5.5).
- [ ] On-device text recognition for scans (macOS Vision, free, nothing leaves the machine): the
      recognised text lets the source and evidence check run on scanned pages too, so most scans
      no longer need review. The model's confidence alone never lets a scan skip review.
- [x] Library or review (`extract/review.py`, `paperclerk extract`, `paperclerk review`): a
      document waits for a person when there's no answer, confidence is under
      `review_threshold`, a value fails the check, or a page wasn't shown; every reason is
      stored. Waiting answers are only shown, marked unconfirmed (F5 lists such bills apart);
      nothing renames or files a document on their strength.
- [x] Results in `extractions`, every read kept as history; the current answer is the newest
      one the user verified, else the newest whose checks still hold. A change in a document's
      text makes its reads stale (verified ones stay), so it's read again. A failed read is
      tried once more on the next run, then waits for review. A new prompt or model doesn't
      re-read documents already read: that's a deliberate re-read (to do: a command for it,
      with a way to skip the cache).
- [ ] Review queue in the web UI: confirm or correct fields; corrected rows marked
      `human_verified`
- [x] `llm_calls` table _(new)_: model, input/output/cached tokens, latency, cost, purpose
      (read / retry / stronger model / second read) for every call; `paperclerk usage` sums it
      per day and model. Still to do: a small usage panel in the UI.
- [x] Spending guard _(new)_: `paperclerk extract` estimates the cost first (on the high side)
      and asks above `llm.confirm_above_usd` ($1); `llm.monthly_budget_usd` stops a run before
      it would pass the month's budget.
- [x] `prompt_version` _(new)_ stored with every extraction, next to the model, so evals can
      compare versions
- [x] Cache by content _(new)_: same content + pages + prompt version + models + effort → the
      earlier read is reused, no call (`extract/cache.py`). Kept forever; failed reads aren't
      cached.
- [ ] Model routing _(new)_: Haiku first, Sonnet for low-confidence results; F6 must show
      accuracy holds
- [x] Prompt caching for the fixed system prompt _(new)_ (`cache_control` on the system
      prompt; the call log shows cached input tokens, to confirm on the first real run)
- [ ] Background jobs _(new)_: scan and extraction run off the request thread; the API returns a
      job id, the UI shows progress; rate limits and retries handled
- [ ] Batch API for the first bulk import _(new, optional)_

**Done when:** F6 reports F2 accuracy; the README shows cost per document before and after
routing and caching.

## F4 — Organize agent

- [ ] The model outputs a `Plan` (data); the UI previews it and the user picks which ops to apply
- [ ] Executor safety rules in code: only inside watched folders, never overwrite, never delete
- [ ] `operations` log; undo by batch
- [ ] LangGraph `interrupt` for approval, SQLite checkpointer, resumable after restart
- [ ] Hand-written and LangGraph versions; comparison recorded in [architecture.md §5](architecture.md#5-where-langgraph-is-used--and-where-it-isnt)
- [ ] Update `document_paths` in the same transaction as each move, so the next scan doesn't see
      a delete plus an add
- [ ] Re-check each file before executing _(new)_: it may have changed or vanished since the plan
- [ ] Validate `suggested_filename` _(new)_: it comes from document content, which may be
      written by someone else. Reject path traversal (`../`), separators and illegal characters.
- [ ] Undo conflicts _(new)_: the original location is taken, or the file changed since the move
- [ ] Injection tests _(new)_: documents whose text tells the agent to move or rename files;
      the executor must refuse anything outside the rules

**Done when:** plan → preview → approve → execute → undo works end to end; all safety tests
pass; the hand-written vs LangGraph comparison is written up.

## F3 — Gmail sync

- [ ] OAuth with the read-only scope
- [ ] Incremental sync with `historyId`
- [ ] Attachments hashed; exact duplicates of local files collapse to one document
- [ ] `document_sources` table: one content can be both in email and on disk, but
      `documents.source` holds only one
- [ ] Email bodies as documents _(new)_: many renewal notices have no attachment
- [ ] Prompt-injection defense _(new)_: anyone can email you, so this is the main attack
      surface. Email content is passed as marked data, separate from instructions; the F4
      executor rules are the second layer; F6 measures attack success rate.
- [ ] Near-duplicates: vendor + amount + date matching, with embedding similarity to find
      candidates first _(new)_ (same invoice as an emailed PDF and a phone photo)
- [ ] OAuth token in the macOS Keychain, not a plain file _(new)_

**Done when:** after syncing a real inbox, an invoice in both email and a folder counts once;
dedupe precision/recall are measured; injection success rate is 0.

## F5 — Bills dashboard and Q&A

- [ ] Dashboard: bills due this month (invoices, plus credit card statements with a due date), subscriptions renewing soon
- [ ] Subscription aggregation _(new — the table exists, the logic doesn't)_: recurring bills
      from one vendor become one subscription
- [ ] LangGraph Q&A with tools `query_bills`, `query_subscriptions`, `search_documents`,
      `read_document`
- [ ] Every answer cites its source documents
- [ ] Hybrid `search_documents` _(new)_: FTS5 keyword search + sqlite-vec vector search, merged
      with reciprocal rank fusion, over per-page chunks from F1; local embedding model
- [ ] Citation check _(new)_: the cited page actually contains the claim
- [ ] Streaming answers _(new)_
- [ ] Dynamic few-shot for F2 _(new)_: retrieve similar `human_verified` documents into the
      extraction prompt; F6 measures the gain
- [ ] Q&A evals _(new)_: question/answer set, LLM-judge grading, retrieval recall@k

**Done when:** the dashboard works on real data; Q&A has eval numbers; hybrid search is compared
against keyword-only.

## Week 6 — Ship

- [ ] MCP server (read-only tools; it can't execute organize plans)
- [ ] Tune on eval results
- [ ] Docker Compose one-command start _(new)_
- [ ] Hosted demo on the synthetic dataset _(new)_
- [ ] README with all key numbers (accuracy, cost per document, latency, injection success rate)
- [ ] Demo video

## F7 — Distillation (stretch)

- [ ] Fine-tune or distill a small local model on Claude outputs plus `human_verified`
      corrections; compare accuracy, cost and speed with F6 ([architecture.md §6](architecture.md#6-thin-llm-interface))

---

## Decisions

The reasoning behind the technical choices is in architecture.md §8–§10.

- **6 weeks, not 4.** If time runs short, cut the hosted demo and model routing first; keep
  injection defense and evals.
- **Local embeddings** (a small model such as bge-small via ONNX, no PyTorch): document text
  never goes to a third party, in line with the privacy goal, at some cost in quality.
- **Eval data:** a public synthetic set plus a small private set of real documents.
- **Distillation** is a stretch goal, outside the 6 weeks.
