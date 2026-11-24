"""F2 step 4: does an answer enter the library, or does a person look first?

A document goes to review when any of these holds, and every reason is recorded in words the
user can act on:

- no answer (every attempt failed);
- the model's confidence is below `review_threshold`;
- a company, amount or date failed the source or evidence check, or two reads disagreed;
- part of the document wasn't shown to the model.

"Library" means the rest of the app may treat the answer as fact: F4 renames and files the
document by it, F5 reminds you to pay by it. An answer waiting for review is only shown, marked
unconfirmed (F5 lists such bills apart); nothing changes files on its strength.
"""

from __future__ import annotations

from paperclerk.extract.classifier import ExtractionResult


def review_reasons(result: ExtractionResult, threshold: float) -> list[str]:
    """Why this document needs a person. Empty: it can enter the library."""
    if result.failed:
        last = result.attempts[-1].error if result.attempts else None
        return [f"no answer from the model ({last or 'not tried'})"]
    reasons = []
    confidence = result.extraction.confidence
    if confidence < threshold:
        reasons.append(f"confidence {confidence:.2f}, below {threshold:.2f}")
    reasons.extend(str(p) for p in result.problems)
    reasons.extend(f"not shown to the model: {u}" for u in result.unread)
    return reasons
