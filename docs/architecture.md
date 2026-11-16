# Architecture & decisions

A running log of design choices and why. Each entry: decision, alternatives, reason.

## 1. One core service, several thin shells

**Decision:** all business logic lives in `paperclerk.core.PaperClerk`. The CLI, the FastAPI app
and the MCP server only translate inputs/outputs.
**Why:** one code path to test; adding an interface never duplicates logic.

## 2. SQLite, local-first

**Alternatives:** Postgres, a hosted vector DB.
**Why:** zero setup for a personal tool, single file, FTS5 for full-text search, `sqlite-vec`
available if embeddings are needed later. Personal financial documents should stay on the device.

## 3. Content hash as document identity

**Decision:** `documents.sha256` is unique; paths can change.
**Why:** exact duplicates (same file in Downloads and as an email attachment) collapse to one row,
and moving a file doesn't create a new document. Near-duplicates (same invoice, different file)
are handled later by matching vendor + amount + date.

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
**To do (week 2):** implement F4 once by hand and once with LangGraph; record the comparison here.

## 6. Thin LLM interface

**Decision:** `llm.base.LLMClient.structured(system, user_text, schema, images)` returns a
validated Pydantic object.
**Why:** swapping Claude for another provider or a small local model (distillation experiment)
touches one package, and evals can compare providers on the same interface.

## 7. Package named `mcp_server`, not `mcp`

**Why:** avoids shadowing the official `mcp` SDK on import.
