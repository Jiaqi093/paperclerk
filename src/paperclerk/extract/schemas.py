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


class Extraction(BaseModel):
    doc_type: DocType
    vendor: str | None = Field(None, description="Who issued the document, e.g. 'Origin Energy'")
    amount: float | None = Field(None, description="Total amount, tax included")
    currency: str | None = Field(None, description="ISO 4217 code, e.g. 'AUD'")
    issue_date: date | None = None
    due_date: date | None = None
    is_recurring: bool | None = None
    period: Period | None = None
    suggested_filename: str | None = Field(
        None, description="e.g. '2026-11_OriginEnergy_Invoice_123.45AUD.pdf'"
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="Model's self-rated confidence")
    reasoning: str | None = Field(None, description="One sentence: why this type and these fields")
