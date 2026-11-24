"""Merging rule hits and model flags into the review queue rows.

The ticket's merge contract, implemented literally:

1. a fragment flagged by both the rule stage and the model becomes **one** record
   with ``detector = 'rule+model'``;
2. a model-only hit with ``confidence < 0.5`` is dropped;
3. a ``quote`` that cannot be found verbatim in the (masked) segment is dropped;
4. everything else is kept - the stated policy is "宁可多标、不能漏标"
   (over-flag rather than miss), so recall wins every tie.

Two hits belong to the same fragment when their spans overlap inside the same
segment; the connected group becomes one record whose span is the union of the
member spans, so the stored quote is always verbatim text of the segment.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .rules import RuleHit
from .schema import ModelFlagHit
from .segments import Segment

REASON_MAX_CHARS = 40
QUOTE_MAX_CHARS = 200
MIN_MODEL_CONFIDENCE = 0.5

DETECTOR_RULE = "rule"
DETECTOR_MODEL = "model"
DETECTOR_BOTH = "rule+model"


def truncate_reason(reason: str, limit: int = REASON_MAX_CHARS) -> str:
    """Keep reasons inside the stored 40-character limit."""

    cleaned = " ".join(reason.split())
    return cleaned[:limit].rstrip() or "未说明理由"


@dataclass(frozen=True)
class Candidate:
    """A fragment that survived the merge and will be written to the queue."""

    seq: int
    quote: str
    start: int
    end: int
    flag_type: str
    detector: str
    confidence: float
    reason: str
    rule_code: str | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class Dropped:
    """A hit the merge refused, with a machine-readable code."""

    seq: int | None
    quote: str
    detector: str
    code: str


@dataclass(frozen=True)
class MergeOutcome:
    kept: tuple[Candidate, ...]
    dropped: tuple[Dropped, ...]
    context_seq_flags: int = 0

    @property
    def dropped_by_code(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.dropped).items()))


@dataclass(frozen=True)
class _Hit:
    detector: str
    start: int
    end: int
    flag_type: str
    reason: str
    confidence: float
    rule_code: str | None


def _locate(segment: Segment, quote: str) -> int | None:
    if not quote:
        return None
    position = segment.text.find(quote)
    return None if position < 0 else position


def merge_fragments(
    segments: Sequence[Segment],
    rule_hits: Sequence[RuleHit] = (),
    model_flags: Sequence[ModelFlagHit] = (),
    *,
    min_model_confidence: float = MIN_MODEL_CONFIDENCE,
    prompt_version: str | None = None,
) -> MergeOutcome:
    """Merge rule hits and model flags into deduplicated candidates."""

    if not 0.0 <= min_model_confidence <= 1.0:
        raise ValueError("min_model_confidence must be inside [0, 1]")
    by_seq = {segment.seq: segment for segment in segments}
    hits_by_seq: dict[int, list[_Hit]] = {}
    dropped: list[Dropped] = []
    context_seq_flags = 0

    for hit in rule_hits:
        segment = by_seq.get(hit.seq)
        if segment is None:
            dropped.append(Dropped(seq=hit.seq, quote=hit.quote, detector=DETECTOR_RULE, code="unknown_seq"))
            continue
        start = _locate(segment, hit.quote)
        if start is None:
            dropped.append(Dropped(seq=hit.seq, quote=hit.quote, detector=DETECTOR_RULE, code="quote_not_found"))
            continue
        hits_by_seq.setdefault(hit.seq, []).append(
            _Hit(
                detector=DETECTOR_RULE,
                start=start,
                end=start + len(hit.quote),
                flag_type=hit.flag_type,
                reason=hit.reason,
                confidence=1.0,
                rule_code=hit.rule_code,
            )
        )

    for flag in model_flags:
        segment = by_seq.get(flag.seq)
        if segment is None:
            dropped.append(Dropped(seq=flag.seq, quote=flag.quote, detector=DETECTOR_MODEL, code="unknown_seq"))
            continue
        if flag.batch_target_seqs and flag.seq not in flag.batch_target_seqs:
            # The prompt forbids flagging context, but recall-first policy keeps
            # such a hit as a diagnostic instead of losing a possibly real flag.
            context_seq_flags += 1
        start = _locate(segment, flag.quote)
        if start is None:
            dropped.append(Dropped(seq=flag.seq, quote=flag.quote, detector=DETECTOR_MODEL, code="quote_not_found"))
            continue
        hits_by_seq.setdefault(flag.seq, []).append(
            _Hit(
                detector=DETECTOR_MODEL,
                start=start,
                end=start + len(flag.quote),
                flag_type=flag.flag_type,
                reason=flag.reason,
                confidence=float(flag.confidence),
                rule_code=None,
            )
        )

    kept: list[Candidate] = []
    for seq in sorted(hits_by_seq):
        segment = by_seq[seq]
        ordered = sorted(hits_by_seq[seq], key=lambda item: (item.start, item.end, item.detector))
        cluster: list[_Hit] = []
        cluster_end = -1
        clusters: list[list[_Hit]] = []
        for hit in ordered:
            if cluster and hit.start < cluster_end:
                cluster.append(hit)
                cluster_end = max(cluster_end, hit.end)
            else:
                if cluster:
                    clusters.append(cluster)
                cluster = [hit]
                cluster_end = hit.end
        if cluster:
            clusters.append(cluster)

        for members in clusters:
            rule_members = sorted(
                (member for member in members if member.detector == DETECTOR_RULE), key=lambda item: item.start
            )
            model_members = sorted(
                (member for member in members if member.detector == DETECTOR_MODEL),
                key=lambda item: (-item.confidence, item.start),
            )
            if not rule_members and model_members and max(m.confidence for m in model_members) < min_model_confidence:
                for member in model_members:
                    dropped.append(
                        Dropped(
                            seq=seq,
                            quote=segment.text[member.start : member.end],
                            detector=DETECTOR_MODEL,
                            code="low_confidence",
                        )
                    )
                continue

            start = min(member.start for member in members)
            end = max(member.end for member in members)
            if end - start > QUOTE_MAX_CHARS:
                end = start + QUOTE_MAX_CHARS

            primary_rule = rule_members[0] if rule_members else None
            primary_model = model_members[0] if model_members else None
            flag_type = primary_rule.flag_type if primary_rule else primary_model.flag_type  # type: ignore[union-attr]
            reason_source = primary_rule.reason if primary_rule and primary_rule.reason else (
                primary_model.reason if primary_model else ""
            )
            if primary_rule and primary_model:
                detector = DETECTOR_BOTH
            elif primary_rule:
                detector = DETECTOR_RULE
            else:
                detector = DETECTOR_MODEL
            confidence = round(max(member.confidence for member in members), 3)
            kept.append(
                Candidate(
                    seq=seq,
                    quote=segment.text[start:end],
                    start=start,
                    end=end,
                    flag_type=flag_type,
                    detector=detector,
                    confidence=confidence,
                    reason=truncate_reason(reason_source),
                    rule_code=primary_rule.rule_code if primary_rule else None,
                    prompt_version=prompt_version if model_members else None,
                )
            )

    kept.sort(key=lambda item: (item.seq, item.start))
    return MergeOutcome(kept=tuple(kept), dropped=tuple(dropped), context_seq_flags=context_seq_flags)


__all__ = [
    "Candidate",
    "DETECTOR_BOTH",
    "DETECTOR_MODEL",
    "DETECTOR_RULE",
    "Dropped",
    "MIN_MODEL_CONFIDENCE",
    "MergeOutcome",
    "merge_fragments",
    "truncate_reason",
]
