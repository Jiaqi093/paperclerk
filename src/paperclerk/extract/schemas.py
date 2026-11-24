"""F2 — the structured output we ask the model for. This schema is also the eval label format."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class DocType(StrEnum):
    INVOICE = "invoice"  # a bill for goods/services, usually with an amount due
    RECEIPT = "receipt"  # proof of a payment already made
    SUBSCRIPTION = "subscription"  # signup/renewal notice for a recurring charge
    STATEMENT = "statement"  # bank / utility / account statement
    CONTRACT = "contract"  # agreements, leases, policies
    IMPORTANT = "important"  # IDs, certificates, tax, legal — keep forever
    WORK = "work"  # employer / company documents
    DISPOSABLE = "disposable"  # marketing, one-off notices, safe to archive
    OTHER = "other"


class Period(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class Evidence(BaseModel):
    """Where each value was read: the shortest exact quote from the document that shows it."""

    vendor: str | None = None
    amount: str | None = None
    issue_date: str | None = None
    due_date: str | None = None


class Extraction(BaseModel):
    doc_type: DocType
    vendor: str | None = Field(None, description="Who issued the document, e.g. 'Origin Energy'")
    amount: float | None = Field(None, description="Depends on the type; see the amount rules")
    currency: str | None = Field(None, description="ISO 4217 code, e.g. 'AUD'")
    issue_date: date | None = None
    due_date: date | None = None
    is_recurring: bool | None = None
    period: Period | None = None
    suggested_filename: str | None = None  # F4 fills this; the model isn't asked for it
    confidence: float = Field(..., ge=0.0, le=1.0, description="Model's self-rated confidence")
    reasoning: str | None = Field(None, description="One sentence: why this type and these fields")
    evidence: Evidence | None = Field(
        None, description="Exact quote from the document for each company, amount and date given"
    )


# --- what the model is asked to return ------------------------------------------------------
# Every field is required but may be null, so the model can't skip one silently, and the order
# is the order it writes in: type and reasoning first, then the quotes, then the values read
# from those quotes. Descriptions point at the rules instead of restating them.


class EvidenceAnswer(BaseModel):
    vendor: str | None = Field(description="Exact quote showing the company, or null")
    amount: str | None = Field(description="Exact quote showing the amount, or null")
    issue_date: str | None = Field(description="Exact quote showing the issue date, or null")
    due_date: str | None = Field(description="Exact quote showing the due date, or null")


class ModelAnswer(BaseModel):
    doc_type: DocType  # a $ref: the API drops descriptions here; the rules say how to pick
    reasoning: str = Field(description="One sentence: why this type and this amount")
    evidence: EvidenceAnswer
    vendor: str | None = Field(description="Who issued it, as the brand people know")
    amount: float | None = Field(description="Depends on the type, see the amount rules; null "
                                 "when they say empty")  # fmt: skip
    currency: str | None = Field(description="Three-letter code, e.g. AUD")
    issue_date: date | None
    due_date: date | None
    is_recurring: bool | None
    period: Period | None
    confidence: float = Field(ge=0.0, le=1.0, description="Chance the whole answer is right")

    def to_extraction(self) -> Extraction:
        return Extraction.model_validate(self.model_dump())
