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
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask, whatever it costs"),
    config: Path = ConfigOpt,
) -> None:
    """Read new documents with the model (this calls the API and costs money)."""
    clerk = _clerk(config)
    count, usd = clerk.estimate_pending(limit=limit)
    if count == 0:
        typer.echo("nothing to read")
        return
    typer.echo(f"{count} document(s) to read, about ${usd:.2f}")
    if usd > clerk.settings.llm.confirm_above_usd and not (
        yes or typer.confirm("That's above your confirm_above_usd. Go ahead?")
    ):
        raise typer.Exit(1)
    r = clerk.classify_pending(limit=limit)
    typer.echo(
        f"read={r.read} (reused={r.reused}) library={r.accepted} review={r.review} "
        f"failed={r.failed} skipped={len(r.skipped)} cost=${r.cost_usd:.4f}"
    )
    for item in r.skipped:
        typer.echo(f"  skipped: {item}", err=True)
    if r.stopped:
        typer.echo(f"stopped: {r.stopped}", err=True)


@app.command()
def usage(config: Path = ConfigOpt) -> None:
    """What model calls cost this month, per day and model."""
    rows = _clerk(config).usage()
    if not rows:
        typer.echo("no model calls this month")
        return
    for r in rows:
        typer.echo(
            f"{r['day']}  {r['model']:<20} calls={r['calls']:<4} failed={r['failed']:<3} "
            f"cost=${r['cost_usd']:.4f}  cached_input={r['cache_read_tokens']}"
        )
    typer.echo(f"total this month: ${sum(r['cost_usd'] for r in rows):.4f}")


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
