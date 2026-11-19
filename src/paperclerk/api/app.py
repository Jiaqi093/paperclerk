"""FastAPI shell over core.PaperClerk, consumed by the Next.js app in web/."""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from paperclerk import __version__
from paperclerk.config import Settings, load_settings
from paperclerk.core import PaperClerk


class Health(BaseModel):
    status: str
    version: str


class DocumentOut(BaseModel):
    id: int
    path: str | None
    source: str
    mime_type: str | None
    size_bytes: int | None
    parse_status: str | None  # ok / unsupported / failed; None = not parsed yet
    parse_error: str | None  # why parsing failed, for the user
    created_at: str


class ScanOut(BaseModel):
    seen: int
    new: int
    updated: int
    removed: int
    skipped: list[str]
    failed: list[str]
    cloud_only: list[str]


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="paperclerk", version=__version__)
    app.state.clerk = PaperClerk(
        settings or load_settings(os.environ.get("PAPERCLERK_CONFIG", "config.toml"))
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[os.environ.get("PAPERCLERK_WEB_ORIGIN", "http://localhost:3000")],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def clerk(request: Request) -> PaperClerk:
        return request.app.state.clerk

    @app.get("/api/health", response_model=Health)
    def health() -> Health:
        return Health(status="ok", version=__version__)

    @app.get("/api/documents", response_model=list[DocumentOut])
    def documents(limit: int = 100, c: PaperClerk = Depends(clerk)) -> list[dict]:
        return c.list_documents(limit=limit)

    @app.post("/api/scan", response_model=ScanOut)
    def scan(c: PaperClerk = Depends(clerk)) -> ScanOut:
        r = c.scan()
        return ScanOut(
            seen=r.seen,
            new=r.new,
            updated=r.updated,
            removed=r.removed,
            skipped=r.skipped,
            failed=r.failed,
            cloud_only=r.cloud_only,
        )

    # Planned: /api/bills, /api/subscriptions (F5)
    #          /api/plans, /api/plans/{id}/approve, /api/batches/{id}/undo (F4)
    return app
