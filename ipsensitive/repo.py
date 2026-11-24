"""Persistence for ``ip_sensitive_flags``.

The repository is tenant-scoped and soft-delete aware: every read filters
``org_id``/``site_id`` and ``deleted_at IS NULL``.  Inserting a fragment twice
(same content, segment and quote) is a no-op, so re-running the pipeline never
duplicates the queue; a soft-deleted row does not block a new one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models.base import utcnow
from .models.flags import DISPOSITIONS, IpSensitiveFlag

if TYPE_CHECKING:  # avoids an import cycle: merge -> schema -> segments is leaf code
    from .moderation.merge import Candidate

REVIEW_TARGETS = ("accepted", "dismissed")


class FlagNotFound(LookupError):
    """No live flag with that id inside the caller's tenant."""


class InvalidDisposition(ValueError):
    """A transition the review queue does not allow."""


@dataclass(frozen=True)
class InsertResult:
    written: int
    skipped: int


class SensitiveFlagRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ insert
    def existing_fragments(self, *, org_id: uuid.UUID, site_id: uuid.UUID, content_id: uuid.UUID) -> set[tuple[int, str]]:
        rows = self.session.execute(
            select(IpSensitiveFlag.segment_seq, IpSensitiveFlag.quote).where(
                IpSensitiveFlag.org_id == org_id,
                IpSensitiveFlag.site_id == site_id,
                IpSensitiveFlag.content_id == content_id,
                IpSensitiveFlag.deleted_at.is_(None),
            )
        ).all()
        return {(int(seq), str(quote)) for seq, quote in rows}

    def insert_candidates(
        self,
        candidates: Sequence["Candidate"],
        *,
        org_id: uuid.UUID,
        site_id: uuid.UUID,
        content_id: uuid.UUID,
        run_id: uuid.UUID,
        created_by: uuid.UUID | None = None,
    ) -> InsertResult:
        existing = self.existing_fragments(org_id=org_id, site_id=site_id, content_id=content_id)
        written = 0
        skipped = 0
        for candidate in candidates:
            if (candidate.seq, candidate.quote) in existing:
                skipped += 1
                continue
            existing.add((candidate.seq, candidate.quote))
            self.session.add(
                IpSensitiveFlag(
                    org_id=org_id,
                    site_id=site_id,
                    content_id=content_id,
                    run_id=run_id,
                    segment_seq=candidate.seq,
                    quote=candidate.quote,
                    quote_start=candidate.start,
                    quote_end=candidate.end,
                    flag_type=candidate.flag_type,
                    detector=candidate.detector,
                    confidence=candidate.confidence,
                    reason=candidate.reason,
                    rule_code=candidate.rule_code,
                    prompt_version=candidate.prompt_version,
                    disposition="pending",
                    created_by=created_by,
                )
            )
            written += 1
        self.session.flush()
        return InsertResult(written=written, skipped=skipped)

    # ------------------------------------------------------------------- reads
    def _live(self, org_id: uuid.UUID, site_id: uuid.UUID):
        return (
            IpSensitiveFlag.org_id == org_id,
            IpSensitiveFlag.site_id == site_id,
            IpSensitiveFlag.deleted_at.is_(None),
        )

    def list_flags(
        self,
        *,
        org_id: uuid.UUID,
        site_id: uuid.UUID,
        content_id: uuid.UUID | None = None,
        disposition: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[IpSensitiveFlag], int]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be inside 1..200")
        if disposition is not None and disposition not in DISPOSITIONS:
            raise ValueError(f"disposition must be one of {DISPOSITIONS}")
        conditions = list(self._live(org_id, site_id))
        if content_id is not None:
            conditions.append(IpSensitiveFlag.content_id == content_id)
        if disposition is not None:
            conditions.append(IpSensitiveFlag.disposition == disposition)

        total = int(
            self.session.scalar(select(func.count()).select_from(IpSensitiveFlag).where(*conditions)) or 0
        )
        items = list(
            self.session.scalars(
                select(IpSensitiveFlag)
                .where(*conditions)
                .order_by(
                    IpSensitiveFlag.content_id,
                    IpSensitiveFlag.segment_seq,
                    IpSensitiveFlag.quote_start,
                    IpSensitiveFlag.id,
                )
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return items, total

    def flags_of_run(self, *, run_id: uuid.UUID, org_id: uuid.UUID, site_id: uuid.UUID) -> list[IpSensitiveFlag]:
        return list(
            self.session.scalars(
                select(IpSensitiveFlag)
                .where(IpSensitiveFlag.run_id == run_id, IpSensitiveFlag.org_id == org_id, IpSensitiveFlag.site_id == site_id)
                .order_by(IpSensitiveFlag.segment_seq, IpSensitiveFlag.quote_start, IpSensitiveFlag.id)
            ).all()
        )

    def get(self, flag_id: uuid.UUID, *, org_id: uuid.UUID, site_id: uuid.UUID) -> IpSensitiveFlag:
        row = self.session.scalars(
            select(IpSensitiveFlag).where(IpSensitiveFlag.id == flag_id, *self._live(org_id, site_id)).limit(1)
        ).first()
        if row is None:
            raise FlagNotFound(str(flag_id))
        return row

    # ------------------------------------------------------------------ writes
    def set_disposition(
        self,
        flag_id: uuid.UUID,
        *,
        org_id: uuid.UUID,
        site_id: uuid.UUID,
        disposition: str,
        reviewed_by: uuid.UUID,
        review_note: str | None = None,
    ) -> IpSensitiveFlag:
        if disposition not in REVIEW_TARGETS:
            raise InvalidDisposition(
                f"staff can only resolve a flag to {REVIEW_TARGETS}; 'pending' is set by the pipeline"
            )
        if review_note is not None and len(review_note) > 500:
            raise ValueError("review_note must be <= 500 characters")
        row = self.get(flag_id, org_id=org_id, site_id=site_id)
        row.disposition = disposition
        row.reviewed_by = reviewed_by
        row.reviewed_at = utcnow()
        row.review_note = review_note
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def soft_delete(self, flag_id: uuid.UUID, *, org_id: uuid.UUID, site_id: uuid.UUID) -> IpSensitiveFlag:
        row = self.get(flag_id, org_id=org_id, site_id=site_id)
        row.deleted_at = utcnow()
        row.updated_at = utcnow()
        self.session.flush()
        return row


__all__ = [
    "FlagNotFound",
    "InsertResult",
    "InvalidDisposition",
    "REVIEW_TARGETS",
    "SensitiveFlagRepository",
]
