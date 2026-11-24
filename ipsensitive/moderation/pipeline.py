"""The re-review pipeline: mask -> batch -> model -> merge -> review queue.

One run is one transaction.  Ordering follows the ticket: mask first (the model
never sees raw identifiers), rules first (the rule stage is upstream), then the
model, then the merge, then the ``ip_sensitive_flags`` rows with
``disposition = 'pending'``.  Nothing is sent anywhere else; the output is an L1
draft that waits for staff disposition.

``detect_document`` is the whole model/rule/merge path without a database, so the
offline evaluation exercises exactly the code the service runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings
from ..repo import InsertResult, SensitiveFlagRepository
from .masking import MaskedDocument, mask_segments
from .merge import MergeOutcome, merge_fragments
from .model_client import ModelClient, ModelError
from .prompt import PromptSpec
from .rules import RegexRuleDetector, RuleHit, RuleSource
from .schema import ModelFlagHit, parse_model_output
from .segments import Segment, SegmentBatch, build_batches, normalise_segments

MODEL_OK = "ok"
MODEL_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ReviewRequest:
    """One draft to re-review.  ``segments`` are the *raw* segments; the pipeline masks them."""

    org_id: uuid.UUID
    site_id: uuid.UUID
    content_id: uuid.UUID
    segments: tuple[Segment, ...]
    rule_hits: tuple[RuleHit, ...] | None = None
    created_by: uuid.UUID | None = None
    roster: tuple[str, ...] = ()
    document_key: str | None = None

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("a review request needs at least one segment")


def build_request(
    *,
    org_id: uuid.UUID,
    site_id: uuid.UUID,
    content_id: uuid.UUID,
    segments: Iterable[Segment | Mapping[str, Any]],
    rule_hits: Iterable[RuleHit | Mapping[str, Any]] | None = None,
    created_by: uuid.UUID | None = None,
    roster: Sequence[str] = (),
    document_key: str | None = None,
) -> ReviewRequest:
    normalised = normalise_segments(segments)
    hits: tuple[RuleHit, ...] | None = None
    if rule_hits is not None:
        hits = tuple(hit if isinstance(hit, RuleHit) else RuleHit(**dict(hit)) for hit in rule_hits)
    return ReviewRequest(
        org_id=org_id,
        site_id=site_id,
        content_id=content_id,
        segments=normalised,
        rule_hits=hits,
        created_by=created_by,
        roster=tuple(roster),
        document_key=document_key,
    )


@dataclass(frozen=True)
class BatchReport:
    index: int
    target_seqs: tuple[int, ...]
    accepted: int
    rejected: int
    rejects: tuple[dict[str, Any], ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class Detection:
    masked: MaskedDocument
    batches: tuple[BatchReport, ...]
    model_flags: tuple[ModelFlagHit, ...]
    rule_hits: tuple[RuleHit, ...]
    outcome: MergeOutcome
    model_status: str
    model_id: str


@dataclass(frozen=True)
class ReviewResult:
    run_id: uuid.UUID
    org_id: uuid.UUID
    site_id: uuid.UUID
    content_id: uuid.UUID
    prompt_version: str
    model_id: str
    model_status: str
    mask_replacements: int
    batches: tuple[BatchReport, ...]
    rule_hits: int
    model_flags: int
    model_rejected: int
    kept: int
    dropped_by_code: dict[str, int]
    context_seq_flags: int
    insert: InsertResult

    @property
    def degraded(self) -> bool:
        return self.model_status != MODEL_OK


def detect_document(
    segments: Sequence[Segment],
    *,
    model_client: ModelClient,
    prompt: PromptSpec,
    settings: Settings,
    rule_source: RuleSource | None = None,
    rule_hits: Sequence[RuleHit] | None = None,
    document_key: str = "",
    roster: Sequence[str] = (),
) -> Detection:
    """Mask, batch, call the model, run the rules and merge - without a database."""

    masked = mask_segments(segments, roster)
    batches = build_batches(
        masked.segments,
        batch_size=prompt.batch_size,
        context_segments=prompt.context_segments,
    )

    model_flags: list[ModelFlagHit] = []
    reports: list[BatchReport] = []
    model_status = MODEL_OK
    for batch in batches:
        try:
            raw_text = model_client.review(document_key=document_key, batch=batch)
        except ModelError as error:
            if settings.on_model_error == "fail":
                raise
            model_status = MODEL_UNAVAILABLE
            reports.append(
                BatchReport(
                    index=batch.index,
                    target_seqs=batch.target_seqs,
                    accepted=0,
                    rejected=0,
                    error=str(error)[:300],
                )
            )
            continue
        parsed = parse_model_output(
            raw_text,
            batch_index=batch.index,
            batch_target_seqs=batch.target_seqs,
            reason_max_chars=prompt.reason_max_chars,
        )
        for flag in parsed.flags:
            model_flags.append(
                ModelFlagHit(
                    seq=flag.seq,
                    quote=flag.quote,
                    flag_type=flag.flag_type,
                    reason=flag.reason,
                    confidence=flag.confidence,
                    batch_index=batch.index,
                    batch_target_seqs=batch.target_seqs,
                )
            )
        reports.append(
            BatchReport(
                index=batch.index,
                target_seqs=batch.target_seqs,
                accepted=len(parsed.flags),
                rejected=parsed.rejected_count,
                rejects=tuple(
                    {"index": item.index, "code": item.code, "detail": item.detail} for item in parsed.rejected
                ),
            )
        )

    resolved_rule_hits: tuple[RuleHit, ...]
    if rule_hits is not None:
        resolved_rule_hits = tuple(rule_hits)
    else:
        detector = rule_source or RegexRuleDetector()
        resolved_rule_hits = tuple(detector.detect(masked.segments))

    outcome = merge_fragments(
        masked.segments,
        resolved_rule_hits,
        model_flags,
        min_model_confidence=settings.min_model_confidence,
        prompt_version=prompt.version,
    )
    return Detection(
        masked=masked,
        batches=tuple(reports),
        model_flags=tuple(model_flags),
        rule_hits=resolved_rule_hits,
        outcome=outcome,
        model_status=model_status,
        model_id=getattr(model_client, "model_id", "unknown"),
    )


class SensitiveReviewer:
    """Runs the re-review for one draft and writes the review queue rows."""

    def __init__(
        self,
        *,
        session: Session,
        model_client: ModelClient,
        prompt: PromptSpec,
        settings: Settings,
        rule_source: RuleSource | None = None,
    ) -> None:
        self.session = session
        self.model_client = model_client
        self.prompt = prompt
        self.settings = settings
        self.rule_source: RuleSource = rule_source or RegexRuleDetector()
        self.repository = SensitiveFlagRepository(session)

    def detect(self, request: ReviewRequest) -> Detection:
        return detect_document(
            request.segments,
            model_client=self.model_client,
            prompt=self.prompt,
            settings=self.settings,
            rule_source=self.rule_source,
            rule_hits=request.rule_hits,
            document_key=request.document_key or str(request.content_id),
            roster=request.roster,
        )

    def run(self, request: ReviewRequest, *, commit: bool = True) -> ReviewResult:
        detection = self.detect(request)
        run_id = uuid.uuid4()
        insert = self.repository.insert_candidates(
            detection.outcome.kept,
            org_id=request.org_id,
            site_id=request.site_id,
            content_id=request.content_id,
            run_id=run_id,
            created_by=request.created_by,
        )
        if commit:
            self.session.commit()
        return ReviewResult(
            run_id=run_id,
            org_id=request.org_id,
            site_id=request.site_id,
            content_id=request.content_id,
            prompt_version=self.prompt.version,
            model_id=detection.model_id,
            model_status=detection.model_status,
            mask_replacements=detection.masked.replacements,
            batches=detection.batches,
            rule_hits=len(detection.rule_hits),
            model_flags=len(detection.model_flags),
            model_rejected=sum(report.rejected for report in detection.batches),
            kept=len(detection.outcome.kept),
            dropped_by_code=detection.outcome.dropped_by_code,
            context_seq_flags=detection.outcome.context_seq_flags,
            insert=insert,
        )


__all__ = [
    "BatchReport",
    "Detection",
    "MODEL_OK",
    "MODEL_UNAVAILABLE",
    "ReviewRequest",
    "ReviewResult",
    "SensitiveReviewer",
    "build_request",
    "detect_document",
]
