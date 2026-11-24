"""``ip_sensitive_flags`` - the review queue written by the re-review pipeline.

One row is one flagged fragment.  The merge contract from the ticket is
enforced by the schema:

* a fragment flagged by both the rule stage and the model is one row with
  ``detector = 'rule+model'`` (and a unique fragment index makes a second row
  impossible);
* only masked text is stored - quotes come from the text sent to the model, so
  no raw personal data lands in the review queue;
* ``disposition`` starts at ``pending``; staff resolve flags through the API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import TimestampMixin, TenantMixin, UUIDPrimaryKeyMixin

FLAG_TYPES: tuple[str, ...] = (
    "third_party_personal",
    "unpublished_business",
    "unlicensed_brand",
    "other",
)
DETECTORS: tuple[str, ...] = ("rule", "model", "rule+model")
DISPOSITIONS: tuple[str, ...] = ("pending", "accepted", "dismissed")

FLAG_TYPE_SQL = ", ".join(f"'{value}'" for value in FLAG_TYPES)
DETECTOR_SQL = ", ".join(f"'{value}'" for value in DETECTORS)
DISPOSITION_SQL = ", ".join(f"'{value}'" for value in DISPOSITIONS)


class IpSensitiveFlag(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "ip_sensitive_flags"

    #: The draft being reviewed.  ``ip_drafts`` belongs to another ticket in the
    #: same programme, so this is a plain UUID with no cross-ticket FK; the
    #: integration point is documented in README.md.
    content_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    #: One re-review run; groups the rows written by a single pass.
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    segment_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    #: Offsets of ``quote`` inside the *masked* segment text (the text the model
    #: saw).  They drive red-flag highlighting in the review queue.
    quote_start: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_end: Mapped[int] = mapped_column(Integer, nullable=False)

    flag_type: Mapped[str] = mapped_column(String(32), nullable=False)
    detector: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    rule_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    disposition: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        CheckConstraint("segment_seq >= 0", name="ck_ip_sensitive_flags_seq"),
        CheckConstraint(
            "quote_start >= 0 AND quote_end > quote_start AND quote_end - quote_start <= 200",
            name="ck_ip_sensitive_flags_span",
        ),
        CheckConstraint(f"flag_type IN ({FLAG_TYPE_SQL})", name="ck_ip_sensitive_flags_flag_type"),
        CheckConstraint(f"detector IN ({DETECTOR_SQL})", name="ck_ip_sensitive_flags_detector"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ip_sensitive_flags_confidence"),
        CheckConstraint("char_length(reason) BETWEEN 1 AND 40", name="ck_ip_sensitive_flags_reason_len"),
        CheckConstraint(f"disposition IN ({DISPOSITION_SQL})", name="ck_ip_sensitive_flags_disposition"),
        CheckConstraint(
            "(disposition = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL)"
            " OR (disposition <> 'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_ip_sensitive_flags_review_state",
        ),
        CheckConstraint("char_length(quote) BETWEEN 1 AND 200", name="ck_ip_sensitive_flags_quote_len"),
        # The review queue is read by content and disposition.
        Index(
            "ix_ip_sensitive_flags_queue",
            "org_id",
            "site_id",
            "content_id",
            "disposition",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_ip_sensitive_flags_run", "run_id"),
        # One live row per fragment: re-running the pipeline cannot duplicate a
        # fragment, whatever detector produced it.  md5() keeps the key small
        # for long quotes.
        Index(
            "uq_ip_sensitive_flags_fragment",
            "content_id",
            "segment_seq",
            text("md5(quote)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


__all__ = [
    "DETECTORS",
    "DISPOSITIONS",
    "FLAG_TYPES",
    "IpSensitiveFlag",
]
