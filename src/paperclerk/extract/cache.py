"""F2 step 5: never pay twice for the same read.

A read is reused when everything that decides its answer and its checks is the same: the
file's content and name, the pages parsed from it, the prompt version (prompt, answer schema,
markup), the models and effort, and the code that builds the input, checks the answer and
decides on a second read (PIPELINE_VERSION, a hash of that code: change a check and nothing
checked the old way is reused). Kept forever: entries are small, and old ones let new versions
be compared with old.

Failed reads aren't kept: the next try should really try.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from paperclerk.extract.classifier import ExtractionResult, extract
from paperclerk.extract.prompt import PROMPT_VERSION
from paperclerk.ingest.parsers.base import Page, ParsedDocument


def _pipeline_version() -> str:
    # Read as package resources, so it works installed from a wheel or a zip too. Any edit to
    # these files, even a comment, means fresh reads: the price of never reusing a read checked
    # by code that has since changed.
    from importlib import resources

    package = resources.files("paperclerk.extract")
    h = hashlib.sha256()
    for name in ("inputs.py", "verify.py", "source_check.py", "classifier.py"):
        h.update(package.joinpath(name).read_bytes())
    return h.hexdigest()[:12]


PIPELINE_VERSION = _pipeline_version()


def pages_marker(pages: Sequence[Page]) -> str:
    """Identifies a document's parsed pages (text and which are scanned)."""
    h = hashlib.sha256()
    for page in pages:
        h.update(f"{int(page.needs_vision)}:{len(page.text)}:".encode())
        h.update(page.text.encode())
    return h.hexdigest()


def cache_key(
    sha256: str,
    pages: Sequence[Page],
    *,
    file_name: str = "",
    model: str,
    vision_model: str | None,
    effort: str | None,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    parts = [
        sha256, pages_marker(pages), Path(file_name).name, prompt_version, PIPELINE_VERSION,
        model, vision_model or "", effort or "",
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def get(conn: sqlite3.Connection, key: str) -> ExtractionResult | None:
    row = conn.execute("SELECT result_json FROM extraction_cache WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return ExtractionResult.from_json(row[0])
    except Exception:  # an entry from an older shape: read again rather than fail
        return None


def put(
    conn: sqlite3.Connection, key: str, result: ExtractionResult, *, commit: bool = True
) -> None:
    if result.failed:
        return
    conn.execute(
        "INSERT OR REPLACE INTO extraction_cache (key, result_json, created_at) VALUES (?, ?, ?)",
        (key, result.to_json(), datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")),
    )
    if commit:
        conn.commit()


def extract_cached(
    conn: sqlite3.Connection | None,
    doc: ParsedDocument,
    data: bytes,
    file_name: str,
    *,
    sha256: str,
    model: str,
    vision_model: str | None,
    effort,
    client_for,
) -> ExtractionResult:
    """`extract`, unless the same read was made before. `conn` None: no cache. For use where
    nothing else shares `conn` (evals); the app uses get/put under its own lock, so the model
    call doesn't hold it."""
    key = cache_key(
        sha256, doc.pages, file_name=file_name, model=model, vision_model=vision_model,
        effort=effort,
    )  # fmt: skip
    if conn is not None:
        hit = get(conn, key)
        if hit is not None:
            return hit
    result = extract(
        doc, data, file_name, model=model, vision_model=vision_model, effort=effort,
        client_for=client_for,
    )  # fmt: skip
    if conn is not None:
        put(conn, key, result)
    return result
