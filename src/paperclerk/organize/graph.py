"""F4 — LangGraph flow: propose plan -> interrupt for human approval -> execute -> log.

Planned nodes (week 2):
    load_context   gather documents + extractions in scope
    propose_plan   LLM returns a `Plan` (schemas.py), validated against safety rules
    review         langgraph `interrupt()`: the UI shows the plan, the user approves a subset
    execute        executor.apply(approved ops) — the only place files are touched
State is checkpointed to SQLite so a plan survives restarts between proposal and approval.
"""

from __future__ import annotations


def build_graph():
    raise NotImplementedError("Organize graph lands in week 2 (F4)")
