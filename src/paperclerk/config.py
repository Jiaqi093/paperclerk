"""Load settings from config.toml (falls back to defaults for tests and first runs)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class LLMSettings(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-5-5"
    review_threshold: float = Field(0.8, ge=0.0, le=1.0)


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


def load_settings(path: str | Path = "config.toml") -> Settings:
    """Read settings from a TOML file. A missing file gives default settings."""
    p = Path(path).expanduser()
    if not p.exists():
        return Settings()
    with p.open("rb") as f:
        return Settings.model_validate(tomllib.load(f))
