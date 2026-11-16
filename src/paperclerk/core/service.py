"""The one place business logic lives. CLI, FastAPI and MCP are thin shells over this class."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from paperclerk.config import Settings
from paperclerk.db import connect, init_db, repo
from paperclerk.ingest.parsers import parser_for
from paperclerk.ingest.scanner import iter_changed_files, sha256_file

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    seen: int = 0
    new: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)  # files a parser couldn't handle yet


class PaperClerk:
    def __init__(self, settings: Settings, conn: sqlite3.Connection | None = None) -> None:
        self.settings = settings
        self.conn = conn or connect(settings.db_path)
        init_db(self.conn)

    # ---- F1: ingest -------------------------------------------------------------------------
    def scan(self) -> ScanResult:
        """Index new or changed files in every watched folder."""
        result = ScanResult()
        for root in self.settings.watched_folders:
            if not root.is_dir():
                log.warning("watched folder missing: %s", root)
                continue
            known = partial(repo.get_mtime_by_path, self.conn)
            for info in iter_changed_files(root, known):
                result.seen += 1
                parser = parser_for(info.path)
                text, mime = None, None
                try:
                    if parser is not None:
                        parsed = parser.parse(info.path)
                        text, mime = parsed.text, parsed.mime_type
                except NotImplementedError:
                    result.skipped.append(str(info.path))
                _, is_new = repo.upsert_document(
                    self.conn,
                    sha256=sha256_file(info.path),
                    path=str(info.path),
                    source="local",
                    mime_type=mime,
                    size_bytes=info.size_bytes,
                    mtime=info.mtime,
                    text=text,
                )
                if is_new:
                    result.new += 1
                else:
                    result.updated += 1
        return result

    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        return repo.list_documents(self.conn, limit=limit)

    # ---- F2: classify / extract ---------------------------------------------------------------
    def classify_pending(self) -> int:
        raise NotImplementedError("week 1 (F2)")

    # ---- F4: organize -------------------------------------------------------------------------
    def propose_plan(self) -> Any:
        raise NotImplementedError("week 2 (F4)")

    def undo(self, batch_id: str) -> int:
        raise NotImplementedError("week 2 (F4)")
