"""Request and response schemas.

The API is the staff-facing review queue plus the trigger for one re-review run.
Tenant identity (``org_id``/``site_id``) and the reviewing staff id come from the
caller: in production the gateway injects them from the authenticated session
(see README, integration points); this ticket deliberately ships no auth of its
own.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models.flags import IpSensitiveFlag
from ..moderation.rules import RuleHit
from ..moderation.schema import FlagType

Disposition = Literal["pending", "accepted", "dismissed"]
ReviewDisposition = Literal["accepted", "dismissed"]


class SegmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=10_000)


class RuleHitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=200)
    flag_type: FlagType
    rule_code: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=40)

    def to_hit(self) -> RuleHit:
        return RuleHit(
            seq=self.seq,
            quote=self.quote,
            flag_type=self.flag_type,
            rule_code=self.rule_code,
            reason=self.reason,
        )


class ReviewIn(BaseModel):
    """One draft to re-review.  The server masks it; never send masked twice."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    site_id: uuid.UUID
    created_by: uuid.UUID | None = None
    segments: list[SegmentIn] = Field(min_length=1, max_length=500)
    #: Omit to use the built-in rule stub; pass the upstream rule stage's hits to
    #: use the real rule results instead.
    rule_hits: list[RuleHitIn] | None = None
    #: Optional roster of names the masking gate should replace with [PII:NAME].
    roster: list[str] = Field(default_factory=list, max_length=100)


class BatchReportOut(BaseModel):
    index: int
    target_seqs: list[int]
    accepted: int
    rejected: int
    rejects: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class FlagOut(BaseModel):
    id: uuid.UUID
    content_id: uuid.UUID
    run_id: uuid.UUID
    segment_seq: int
    quote: str
    quote_start: int
    quote_end: int
    flag_type: str
    detector: str
    confidence: float
    reason: str
    rule_code: str | None
    prompt_version: str | None
    disposition: str
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: IpSensitiveFlag) -> "FlagOut":
        return cls(
            id=row.id,
            content_id=row.content_id,
            run_id=row.run_id,
            segment_seq=row.segment_seq,
            quote=row.quote,
            quote_start=row.quote_start,
            quote_end=row.quote_end,
            flag_type=row.flag_type,
            detector=row.detector,
            confidence=float(row.confidence),
            reason=row.reason,
            rule_code=row.rule_code,
            prompt_version=row.prompt_version,
            disposition=row.disposition,
            reviewed_by=row.reviewed_by,
            reviewed_at=row.reviewed_at,
            review_note=row.review_note,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ReviewOut(BaseModel):
    run_id: uuid.UUID
    content_id: uuid.UUID
    prompt_version: str
    model_id: str
    model_status: str
    degraded: bool
    mask_replacements: int
    batches: list[BatchReportOut]
    rule_hits: int
    model_flags: int
    model_rejected: int
    kept: int
    dropped_by_code: dict[str, int]
    context_seq_flags: int
    written: int
    skipped_duplicates: int
    flags: list[FlagOut]


class FlagPage(BaseModel):
    items: list[FlagOut]
    total: int
    limit: int
    offset: int


class DispositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    site_id: uuid.UUID
    disposition: ReviewDisposition
    reviewed_by: uuid.UUID
    review_note: str | None = Field(default=None, max_length=500)


class HealthOut(BaseModel):
    status: str
    prompt_version: str
    prompt_batch_size: int
    model_mode: str
    min_model_confidence: float


__all__ = [
    "BatchReportOut",
    "Disposition",
    "DispositionIn",
    "FlagOut",
    "FlagPage",
    "HealthOut",
    "ReviewDisposition",
    "ReviewIn",
    "ReviewOut",
    "RuleHitIn",
    "SegmentIn",
]
