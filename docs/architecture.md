# Architecture & decisions

A running log of design choices and why. Each entry: decision, alternatives, reason.

## 1. One core service, several thin shells

**Decision:** all business logic lives in `paperclerk.core.PaperClerk`. The CLI, the FastAPI app
and the MCP server only translate inputs/outputs.
**Why:** one code path to test; adding an interface never duplicates logic.

## 2. SQLite, local-first

**Alternatives:** Postgres, a hosted vector DB.
**Why:** zero setup for a personal tool, single file, FTS5 for full-text search, `sqlite-vec`
for embeddings (see §9). Personal financial documents should stay on the device.

## 3. Content hash as document identity

**Decision:** `documents.sha256` is unique; paths can change.
**Why:** exact duplicates (same file in Downloads and as an email attachment) collapse to one row,
and moving a file doesn't create a new document. Near-duplicates (same invoice, different file)
are handled later by matching vendor + amount + date.

**Paths are a separate table (`document_paths`).** A document is immutable content: its
sha256, text and parse outcome never change under the same id, so extractions keyed on
`document_id` can't go stale. `document_paths` maps each on-disk path to the document it
currently holds: copies are several paths to one document; saving next month's bill under the
same filename repoints that path to a new document and keeps last month's as history; deleted
files lose their path row but keep their document. `documents.path` is legacy.
**Alternatives:** one `path` column per document (can't represent copies, so they flip-flopped
on every scan, and editing a file either duplicated rows or rewrote the old content's row).

## 3a. Schema migrations

**Decision:** `schema.sql` is the frozen base; changes are appended to `MIGRATIONS` in
`db/connection.py` and tracked with `PRAGMA user_version`, one `BEGIN IMMEDIATE` transaction
per step with the version re-read under the lock, so the CLI and API starting together on an
old database don't apply a step twice.
**Why:** `CREATE TABLE IF NOT EXISTS` never alters existing databases; a migration library
would be heavier than this project needs.

## 4. Plans are data; only the executor touches files

**Decision:** the organize agent outputs a `Plan` (list of `PlannedOp`). A separate executor
validates every op (inside watched folders, no overwrite, no delete), applies approved ops,
and logs them to `operations` for undo.
**Why:** the model can be wrong; safety rules are enforced in code, not in the prompt.

## 5. Where LangGraph is used — and where it isn't

**Decision:** LangGraph for the organize flow (F4) and Q&A agent (F5) only. Ingest (F1) and
extraction (F2) are plain functions.
**Why:** F4 needs pause-for-human-approval and resumable state (LangGraph `interrupt` +
SQLite checkpointer). F1/F2 are single-shot pipelines; a framework would add indirection
without benefit. Nodes call the Anthropic SDK directly rather than LangChain model wrappers.
**To do (F4):** implement F4 once by hand and once with LangGraph; record the comparison here.

## 6. Thin LLM interface

**Decision:** `llm.base.LLMClient.structured(system, user_text, schema, images)` returns a
validated Pydantic object.
**Why:** swapping Claude for another provider or a small local model (distillation experiment)
touches one package, and evals can compare providers on the same interface.

## 7. Package named `mcp_server`, not `mcp`

**Why:** avoids shadowing the official `mcp` SDK on import.

## 8. Evals before tuning

**Decision:** the F6 harness starts alongside F2, not after it. Planned: scoring runs in CI on
cached model outputs; runs that call the API are manual. Every run records cost and latency as well
as accuracy.
**Why:** prompt, model-routing and caching choices in F2 should be made from numbers. Cached
outputs keep CI free and deterministic.

## 9. Local embeddings, hybrid search

**Decision:** F5 `search_documents` combines FTS5 keyword search with `sqlite-vec` vector
search (reciprocal rank fusion) over per-page chunks, with a small local embedding model
(e.g. bge-small via ONNX).
**Alternatives:** an embedding API (better quality, but sends document text to another
provider); keyword search only (misses paraphrases like "power bill" vs "electricity").
**Why:** keeps documents on the device; bills are short and keyword-heavy, so a small model
plus FTS5 should be enough. F6 compares hybrid against keyword-only.

## 10. Untrusted document content

**Decision:** document and email text is treated as untrusted input. It is passed to the model
as marked data, never as instructions, and model output that touches the filesystem (paths,
filenames) is validated by the executor.
**Why:** anyone can email you a "bill". Prompt injection is the main attack surface; the
executor's rules in code (§4) are the layer that can't be talked out of.

## 11. PDF library: pypdfium2, not PyMuPDF

**Decision:** parse PDFs with pypdfium2 (PDFium, Apache-2.0/BSD).
**Alternatives:** PyMuPDF (equally capable, but AGPL-3.0: depending on it would put the whole
project, including a hosted demo, under AGPL terms, contradicting the MIT license); pypdf
(pure Python, weaker text extraction, can't render pages for the vision model).
**Why:** same capabilities this project needs (text per page, image positions for scan
detection, rendering pages to images for F2) under a license compatible with MIT.

**Scan detection is per page:** a page needs vision when it has under 30 visible characters,
or when images cover at least half of it and it has at most 200 characters (a phone scan with
a stamped line of text). Mixed PDFs then send only their scanned pages as images. Thresholds
are starting points to tune on the F6 eval set.
