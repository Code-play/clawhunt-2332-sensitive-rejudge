"""Segmentation and the 30 + 1/1 batching contract."""

from __future__ import annotations

import pytest

from ipsensitive.moderation.segments import (
    MAX_SEGMENT_CHARS,
    Segment,
    build_batches,
    normalise_segments,
)


def _segments(count: int) -> list[Segment]:
    return [Segment(seq=index, text=f"第{index}段。") for index in range(count)]


def test_normalise_orders_and_accepts_mappings():
    segments = normalise_segments([{"seq": 2, "text": "三"}, Segment(seq=0, text="一"), {"seq": 1, "text": "二"}])
    assert [segment.seq for segment in segments] == [0, 1, 2]
    assert [segment.text for segment in segments] == ["一", "二", "三"]


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"seq": 0, "text": "a"}, {"seq": 0, "text": "b"}],
        [{"seq": -1, "text": "a"}],
    ],
)
def test_normalise_rejects_invalid_input(items):
    with pytest.raises(ValueError):
        normalise_segments(items)


def test_segment_rejects_oversized_text():
    with pytest.raises(ValueError):
        Segment(seq=0, text="x" * (MAX_SEGMENT_CHARS + 1))


def test_thirty_segments_are_one_batch_without_context():
    batches = build_batches(_segments(30))
    assert len(batches) == 1
    batch = batches[0]
    assert batch.index == 0
    assert batch.target_seqs == tuple(range(30))
    assert batch.context_before is None and batch.context_after is None
    assert len(batch.segments) == 30


def test_thirty_one_segments_are_two_batches_with_one_context_each():
    batches = build_batches(_segments(31))
    assert [len(batch.target_seqs) for batch in batches] == [30, 1]
    assert batches[0].context_after is not None and batches[0].context_after.seq == 30
    assert batches[0].context_before is None
    assert batches[1].context_before is not None and batches[1].context_before.seq == 29
    assert batches[1].context_after is None


def test_middle_batch_has_context_on_both_sides():
    batches = build_batches(_segments(61))
    assert len(batches) == 3
    middle = batches[1]
    assert middle.target_seqs[0] == 30 and middle.target_seqs[-1] == 59
    assert middle.context_before is not None and middle.context_before.seq == 29
    assert middle.context_after is not None and middle.context_after.seq == 60
    assert len(middle.segments) == 32


def test_payload_marks_roles_and_target_seqs():
    batch = build_batches(_segments(31))[1]
    payload = batch.as_payload("ip.sensitive.v1")
    assert payload["target_seqs"] == [30]
    assert payload["segments"][0] == {"seq": 29, "role": "context_before", "text": "第29段。"}
    assert payload["segments"][1] == {"seq": 30, "role": "target", "text": "第30段。"}
    assert batch.role_of(30) == "target" and batch.role_of(29) == "context_before"


def test_batching_rejects_empty_input():
    with pytest.raises(ValueError):
        build_batches([])
