"""F3 — read-only Gmail sync (scope: gmail.readonly), feeding the same F1/F2 pipeline.

Planned (week 3): OAuth installed-app flow, incremental sync via historyId,
attachments hashed so the same invoice in email and on disk dedupes to one document.
"""

from __future__ import annotations


def sync() -> int:
    raise NotImplementedError("Gmail sync lands in week 3 (F3)")
