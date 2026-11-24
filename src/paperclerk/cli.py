"""Command-line shell over core.PaperClerk."""

from __future__ import annotations

from pathlib import Path

import typer

from paperclerk.config import load_settings
from paperclerk.core import PaperClerk

app = typer.Typer(help="paperclerk — local-first AI clerk for your documents and bills.")

ConfigOpt = typer.Option(Path("config.toml"), "--config", "-c", help="Path to config.toml")


def _clerk(config: Path) -> PaperClerk:
    return PaperClerk(load_settings(config))


@app.command()
def init(config: Path = ConfigOpt) -> None:
    """Create the local database."""
    clerk = _clerk(config)
    typer.echo(f"Database ready at {clerk.settings.db_path}")


@app.command()
def scan(config: Path = ConfigOpt) -> None:
    """Index new or changed files in the watched folders."""
    r = _clerk(config).scan()
    typer.echo(
        f"seen={r.seen} new={r.new} updated={r.updated} removed={r.removed} "
        f"skipped={len(r.skipped)} failed={len(r.failed)} cloud_only={len(r.cloud_only)}"
    )
    for path in r.failed:
        typer.echo(f"  failed: {path} ({r.reasons.get(path, 'unknown')})", err=True)


@app.command()
def docs(limit: int = 20, config: Path = ConfigOpt) -> None:
    """List indexed documents."""
    for d in _clerk(config).list_documents(limit=limit):
        typer.echo(f"{d['id']:>5}  {d['source']:<6} {d['path']}")


@app.command()
def extract(
    limit: int = typer.Option(None, help="Read at most this many documents"),
    config: Path = ConfigOpt,
) -> None:
    """Read new documents with the model (this calls the API and costs money)."""
    r = _clerk(config).classify_pending(limit=limit)
    typer.echo(
        f"read={r.read} library={r.accepted} review={r.review} failed={r.failed} "
        f"skipped={len(r.skipped)} cost=${r.cost_usd:.4f}"
    )
    for path in r.skipped:
        typer.echo(f"  skipped: {path} (file changed or gone; scan first)", err=True)


@app.command()
def review(config: Path = ConfigOpt) -> None:
    """List documents waiting for you, with the reasons."""
    for item in _clerk(config).review_queue():
        what = " ".join(
            str(item[k]) for k in ("doc_type", "vendor", "amount", "due_date") if item.get(k)
        )
        typer.echo(f"{item['document_id']:>5}  {what or '(no answer)'}")
        for reason in item["reasons"]:
            typer.echo(f"         - {reason}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the FastAPI backend for the web UI."""
    import uvicorn

    uvicorn.run("paperclerk.api.app:create_app", host=host, port=port, factory=True, reload=False)


if __name__ == "__main__":
    app()
