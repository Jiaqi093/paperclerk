"""Load settings from config.toml (falls back to defaults for tests and first runs)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from paperclerk.llm.base import Effort


class LLMSettings(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-5-5"
    # How much the model thinks before answering: low, medium, high, xhigh, max. Unset: the
    # model's own default. Chosen from eval results.
    effort: Effort | None = None
    # Reads documents with scanned pages (images are harder to read). "" turns it off: `model`
    # reads them too.
    vision_model: str | None = "claude-opus-5-5"
    review_threshold: float = Field(0.8, ge=0.0, le=1.0)
    # `paperclerk extract` asks before a run expected to cost more than this (USD).
    confirm_above_usd: float = Field(1.0, ge=0.0)
    # Stop reading for the rest of the month once model calls have cost this much (USD).
    # Unset: no limit.
    monthly_budget_usd: float | None = Field(None, ge=0.0)


class Settings(BaseModel):
    watched_folders: list[Path] = Field(default_factory=list)
    library_root: Path | None = None
    db_path: Path = Path("~/.paperclerk/paperclerk.db")
    llm: LLMSettings = Field(default_factory=LLMSettings)

    @field_validator("watched_folders", mode="after")
    @classmethod
    def _expand_folders(cls, v: list[Path]) -> list[Path]:
        return [p.expanduser().resolve() for p in v]

    @field_validator("library_root", "db_path", mode="after")
    @classmethod
    def _expand_path(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None


def load_env(path: str | Path) -> None:
    """Put KEY=value lines from a .env file into the environment, without overriding what is
    already set (an exported ANTHROPIC_API_KEY wins). Missing file: nothing to do."""
    p = Path(path).expanduser()
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():  # -sig: drop a BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value[0] in value[1:]:
            value = value[1 : value.index(value[0], 1)]  # quoted: up to the closing quote
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()  # an inline comment
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings(path: str | Path = "config.toml") -> Settings:
    """Read settings from a TOML file. A missing file gives default settings. A .env file next
    to it (API keys) is loaded into the environment."""
    p = Path(path).expanduser()
    load_env(p.parent / ".env")
    if not p.exists():
        return Settings()
    with p.open("rb") as f:
        return Settings.model_validate(tomllib.load(f))
