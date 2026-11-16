"""F4 — an organize plan is data, not actions.

The agent proposes; only the executor touches files.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class OpKind(StrEnum):
    MOVE = "move"      # move (and optionally rename) a file
    RENAME = "rename"  # rename in place
    MKDIR = "mkdir"    # create a folder
    # No DELETE on purpose: "disposable" files are moved to a To-Delete folder for the user.


class PlannedOp(BaseModel):
    op: OpKind
    document_id: int | None = None
    src_path: str | None = None
    dst_path: str
    reason: str = Field(..., description="Shown to the user in the preview")


class Plan(BaseModel):
    ops: list[PlannedOp]
    summary: str
