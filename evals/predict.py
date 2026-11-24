"""Read the labelled documents with F2 and write the answers F6 scores.

    uv run python evals/predict.py --labels evals/data/real/labels.jsonl
    uv run python evals/run_eval.py --labels evals/data/real/labels.jsonl \\
        --predictions evals/data/real/predictions.jsonl

This calls the model and costs money: it says about how much first and asks above
`llm.confirm_above_usd`. Reads are kept in a cache (evals/data/.eval-cache.sqlite), so running
it again with the same prompt, models and effort costs nothing. The model, vision model and
effort come from config.toml unless given here, so one config can be compared with another.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from paperclerk.config import load_settings
from paperclerk.db import connect, init_db, repo
from paperclerk.evals.data import load_labels
from paperclerk.extract.cache import cache_key, extract_cached, get
from paperclerk.extract.estimate import document_cost
from paperclerk.ingest.parsers import ParseError, parser_for
from paperclerk.ingest.parsers.base import ParsedDocument

EVALS = Path(__file__).parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="default: predictions.jsonl next to the labels")
    ap.add_argument("--config", type=Path, default=Path("config.toml"))
    ap.add_argument("--model", help="default: llm.model in config")
    ap.add_argument("--vision-model", help='default: llm.vision_model in config; "" = --model')
    ap.add_argument("--effort", help="default: llm.effort in config")
    ap.add_argument("--limit", type=int, help="only the first N labelled documents")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask, whatever it costs")
    ap.add_argument("--cache", type=Path, default=EVALS / "data" / ".eval-cache.sqlite")
    args = ap.parse_args()

    llm = load_settings(args.config).llm
    model = args.model or llm.model
    vision = (args.vision_model if args.vision_model is not None else llm.vision_model) or None
    effort = args.effort or llm.effort
    labels = load_labels(args.labels)[: args.limit]
    root = args.labels.parent
    out = args.out or root / "predictions.jsonl"

    conn = connect(args.cache)
    init_db(conn)

    docs: list[tuple[str, ParsedDocument | None, bytes, str | None]] = []
    estimate = 0.0
    for lab in labels:
        path = root / lab.file
        try:
            data = path.read_bytes()
            parser = parser_for(path)
            if parser is None:
                raise ParseError("unsupported file type")
            doc = parser.parse(data, path)
        except (OSError, ParseError) as e:
            docs.append((lab.file, None, b"", str(e)))
            continue
        sha = hashlib.sha256(data).hexdigest()
        key = cache_key(
            sha, doc.pages, file_name=Path(lab.file).name, model=model, vision_model=vision,
            effort=effort,
        )  # fmt: skip
        if get(conn, key) is None:
            estimate += document_cost(doc, model=model, vision_model=vision)
        docs.append((lab.file, doc, data, sha))

    print(f"{len(docs)} documents, model {model}, vision {vision or model}, effort {effort}")
    print(f"about ${estimate:.2f} (reads already made are free)")
    if estimate > llm.confirm_above_usd and not args.yes:
        if input("That's above confirm_above_usd. Go ahead? [y/N] ").strip().lower() != "y":
            sys.exit(1)

    from paperclerk.llm.anthropic_client import client_for

    spent, reused, failed = 0.0, 0, 0
    result = None

    def reused_now(r) -> bool:
        return r is not None and r.cached

    # Written next to the old file and moved over it at the end: stopping half-way (Ctrl-C)
    # leaves the previous predictions as they were.
    partial = out.with_name(out.name + ".partial")
    with partial.open("w", encoding="utf-8") as f:
        for i, (name, doc, data, sha_or_error) in enumerate(docs, start=1):
            row = {"file": name, "output": None, "error": None, "model": None,
                   "prompt_version": None, "cost_usd": None, "latency_ms": None}  # fmt: skip
            result = None
            if doc is None:
                row["error"] = f"couldn't parse: {sha_or_error}"
                failed += 1
            else:
                start = time.monotonic()
                try:
                    result = extract_cached(
                        conn, doc, data, Path(name).name, sha256=sha_or_error, model=model,
                        vision_model=vision, effort=effort, client_for=client_for,
                    )  # fmt: skip
                except Exception as e:  # one document's trouble never stops the run
                    row["error"] = f"{type(e).__name__}: {e}"[:300]
                    failed += 1
                else:
                    repo.insert_llm_calls(conn, None, result)
                    reused += result.cached
                    if not result.cached:
                        spent += result.cost_usd or 0.0
                    # What a read of this document costs and takes, even when this run reused
                    # it: the scorecard reports the pipeline, not this run's bill.
                    row.update(
                        model=result.model or result.requested_model,
                        prompt_version=result.prompt_version,
                        cost_usd=result.original_cost_usd if result.cached else result.cost_usd,
                        latency_ms=(
                            result.original_latency_ms if result.cached else result.latency_ms
                        )
                        or int((time.monotonic() - start) * 1000),
                    )
                    if result.failed:
                        failed += 1
                        row["error"] = result.attempts[-1].error if result.attempts else "failed"
                    else:
                        row["output"] = result.extraction.model_dump(mode="json")
            f.write(json.dumps(row) + "\n")
            how = "reused" if doc is not None and reused_now(result) else "read"
            print(f"  [{i}/{len(docs)}] {how}{'  FAILED' if row['error'] else ''}", flush=True)

    partial.replace(out)
    print(f"\nwrote {out}: {len(docs)} answers, {reused} reused, {failed} failed, "
          f"spent ${spent:.4f}")  # fmt: skip
    print(f"score: uv run python evals/run_eval.py --labels {args.labels} --predictions {out}")


if __name__ == "__main__":
    main()
