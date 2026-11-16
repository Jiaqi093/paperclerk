from __future__ import annotations

from pathlib import Path

import pytest

from paperclerk.config import Settings
from paperclerk.core import PaperClerk
from paperclerk.db import connect


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    root = tmp_path / "inbox"
    root.mkdir()
    (root / "note.txt").write_text("Electricity bill due 24 Oct, $214.37", encoding="utf-8")
    (root / "receipt.png").write_bytes(b"\x89PNG fake image bytes")
    (root / "ignored.exe").write_bytes(b"not a document")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "secret.txt").write_text("skip me", encoding="utf-8")
    return root


@pytest.fixture
def clerk(inbox: Path) -> PaperClerk:
    return PaperClerk(Settings(watched_folders=[inbox]), conn=connect(":memory:"))
