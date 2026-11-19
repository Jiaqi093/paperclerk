# paperclerk

> A local-first AI agent that finds your invoices, bills and subscriptions across email and folders — and organizes them safely, with preview and undo.

**Status:** early development — F1 ingest in progress. See the roadmap below.

## Why

Personal admin is scattered: an invoice in a Gmail attachment, the same invoice in Downloads,
a renewal notice buried in the inbox. paperclerk pulls these together, extracts what matters
(vendor, amount, due date, renewal period), and proposes a tidy folder structure that you
approve before anything moves. Your documents never leave your machine except for the model call.

## Architecture

```
            ┌──────────── web/ (Next.js) ────────────┐
            │  Documents · Bills · Organize preview   │
            └───────────────────┬─────────────────────┘
                                │ REST
 CLI (Typer) ──┐        ┌───────▼────────┐        ┌── MCP server (Claude Desktop, Cursor)
               └───────►│  core/service  │◄───────┘
                        └───────┬────────┘
     ┌──────────────┬───────────┼──────────────┬───────────────┐
  ingest/ (F1)  extract/ (F2)  sources/ (F3)  organize/ (F4)  ask/ (F5)
  scan, hash,   classify +     Gmail          LangGraph:      LangGraph:
  parse         structured     read-only      plan → approve  tool-using Q&A
                extraction                    → execute/undo  with citations
                        └───────────┬────────────┘
                              SQLite (db/)
```

Design decisions and trade-offs live in [docs/architecture.md](docs/architecture.md).

## Features (MVP)

| # | Feature | Status |
|---|---|---|
| F1 | Local file ingest & incremental indexing | 🟡 scanner + text/image parsers; PDF/DOCX next |
| F2 | Classification & structured extraction with confidence + review queue | ⚪ |
| F3 | Gmail (read-only) sync, cross-source dedupe | ⚪ |
| F4 | Organize agent: plan → preview → approve → execute, with undo | ⚪ |
| F5 | Bills dashboard + Q&A with citations | ⚪ |
| F6 | Evals: classification / extraction / dedupe accuracy | ⚪ |
| + | MCP server | ⚪ |
| F7 | Distillation to a small local model (stretch) | ⚪ |

## Eval results

_First numbers in week 2._ See [evals/README.md](evals/README.md) for methodology.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22+.

```bash
# backend
cp config.example.toml config.toml   # set watched_folders
cp .env.example .env                  # set ANTHROPIC_API_KEY
uv sync --group dev
uv run paperclerk init
uv run paperclerk scan
uv run paperclerk serve               # http://127.0.0.1:8000/docs

# frontend (new terminal)
cd web
cp .env.example .env.local
npm install
npm run dev                           # http://localhost:3000
```

Tests: `uv run pytest`

## Privacy

- Real documents, the database, `config.toml` and `.env` are git-ignored.
- The agent can only touch files inside `watched_folders`; it never deletes.
- Gmail access is read-only.

## Roadmap

Full plan with per-stage checklists: [docs/roadmap.md](docs/roadmap.md).

- **Week 1** — finish F1 (PDF/DOCX, per-page text), F2 basics (LLM client, call log, extraction), F6 v1 (start labelling eval data, synthetic set)
- **Week 2** — finish F2 (review UI, background jobs, model routing, caching), first eval numbers
- **Week 3** — F4 organize agent with undo (hand-written vs LangGraph), safety and injection tests
- **Week 4** — F3 Gmail sync, cross-source dedupe, prompt-injection defense
- **Week 5** — F5 hybrid search (FTS5 + vectors), Q&A with citations, bills dashboard
- **Week 6** — MCP server, tune on eval results, Docker, hosted demo on synthetic data, demo video
- **Later** — F7 distillation (stretch)

## License

MIT
