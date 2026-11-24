"""Segmentation and batching for the model re-review.

A draft arrives as ordered segments (paragraphs).  The model is asked to judge
them in batches of at most ``batch_size`` (30 per the ticket), and every batch
carries one context segment before and after the target window so the model can
resolve pronouns and references without seeing the whole draft.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_SEGMENT_CHARS = 10_000

ROLE_TARGET = "target"
ROLE_CONTEXT_BEFORE = "context_before"
ROLE_CONTEXT_AFTER = "context_after"


@dataclass(frozen=True)
class Segment:
    """One paragraph of a draft, identified by its position ``seq``."""

    seq: int
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.seq, int) or isinstance(self.seq, bool):
            raise ValueError(f"segment seq must be an int, got {self.seq!r}")
        if self.seq < 0:
            raise ValueError(f"segment seq must be >= 0, got {self.seq}")
        if not isinstance(self.text, str):
            raise ValueError(f"segment {self.seq}: text must be a string")
        if len(self.text) > MAX_SEGMENT_CHARS:
            raise ValueError(f"segment {self.seq}: text exceeds {MAX_SEGMENT_CHARS} characters")


@dataclass(frozen=True)
class SegmentBatch:
    """A model call: target segments plus at most one context segment per side."""

    index: int
    target_seqs: tuple[int, ...]
    segments: tuple[Segment, ...]
    context_before: Segment | None = None
    context_after: Segment | None = None

    def role_of(self, seq: int) -> str:
        if seq in self.target_seqs:
            return ROLE_TARGET
        if self.context_before is not None and seq == self.context_before.seq:
            return ROLE_CONTEXT_BEFORE
        if self.context_after is not None and seq == self.context_after.seq:
            return ROLE_CONTEXT_AFTER
        raise KeyError(f"segment {seq} is not part of batch {self.index}")

    def target_segments(self) -> tuple[Segment, ...]:
        return tuple(segment for segment in self.segments if segment.seq in self.target_seqs)

    def as_payload(self, prompt_version: str) -> dict[str, Any]:
        return {
            "prompt_version": prompt_version,
            "batch_index": self.index,
            "target_seqs": list(self.target_seqs),
            "segments": [
                {"seq": segment.seq, "role": self.role_of(segment.seq), "text": segment.text}
                for segment in self.segments
            ],
        }


def normalise_segments(items: Iterable[Segment | Mapping[str, Any]]) -> tuple[Segment, ...]:
    """Validate and order segments; duplicate or negative ``seq`` is rejected."""

    segments: list[Segment] = []
    seen: set[int] = set()
    for item in items:
        segment = item if isinstance(item, Segment) else Segment(seq=int(item["seq"]), text=str(item["text"]))
        if segment.seq in seen:
            raise ValueError(f"duplicate segment seq {segment.seq}")
        seen.add(segment.seq)
        segments.append(segment)
    if not segments:
        raise ValueError("a draft must contain at least one segment")
    segments.sort(key=lambda segment: segment.seq)
    return tuple(segments)


def build_batches(
    segments: Sequence[Segment],
    *,
    batch_size: int = 30,
    context_segments: int = 1,
) -> tuple[SegmentBatch, ...]:
    """Split ``segments`` into ``batch_size`` windows with context on both sides."""

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if context_segments < 0:
        raise ValueError("context_segments must be >= 0")
    ordered = list(segments)
    if not ordered:
        raise ValueError("cannot batch an empty draft")

    batches: list[SegmentBatch] = []
    for index, offset in enumerate(range(0, len(ordered), batch_size)):
        window = ordered[offset : offset + batch_size]
        before = ordered[offset - 1] if context_segments and offset > 0 else None
        after_index = offset + batch_size
        after = ordered[after_index] if context_segments and after_index < len(ordered) else None
        members: list[Segment] = []
        if before is not None:
            members.append(before)
        members.extend(window)
        if after is not None:
            members.append(after)
        batches.append(
            SegmentBatch(
                index=index,
                target_seqs=tuple(segment.seq for segment in window),
                segments=tuple(members),
                context_before=before,
                context_after=after,
            )
        )
    return tuple(batches)


__all__ = [
    "MAX_SEGMENT_CHARS",
    "ROLE_CONTEXT_AFTER",
    "ROLE_CONTEXT_BEFORE",
    "ROLE_TARGET",
    "Segment",
    "SegmentBatch",
    "build_batches",
    "normalise_segments",
]
