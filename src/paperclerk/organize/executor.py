"""F4 — apply approved ops and record them in `operations`; undo a batch in reverse order.

Safety rules (enforced here, not trusted from the model):
- src and dst must stay inside the configured watched folders
- never overwrite an existing file
- no deletes
"""

from __future__ import annotations

import sqlite3

from paperclerk.organize.schemas import PlannedOp


def apply(conn: sqlite3.Connection, ops: list[PlannedOp]) -> str:
    """Apply ops, return batch_id."""
    raise NotImplementedError("Executor not implemented yet (F4)")


def undo(conn: sqlite3.Connection, batch_id: str) -> int:
    """Reverse a batch. Returns number of ops undone."""
    raise NotImplementedError("Undo not implemented yet (F4)")
