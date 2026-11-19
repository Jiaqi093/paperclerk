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
        typer.echo(f"  failed: {path}", err=True)


@app.command()
def docs(limit: int = 20, config: Path = ConfigOpt) -> None:
    """List indexed documents."""
    for d in _clerk(config).list_documents(limit=limit):
        typer.echo(f"{d['id']:>5}  {d['source']:<6} {d['path']}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the FastAPI backend for the web UI."""
    import uvicorn

    uvicorn.run("paperclerk.api.app:create_app", host=host, port=port, factory=True, reload=False)


if __name__ == "__main__":
    app()
