"""The prompt file is the contract: version, batching, output schema, anchors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ipsensitive.moderation.prompt import (
    EXPECTED_BATCH_SIZE,
    EXPECTED_CONTEXT_SEGMENTS,
    PAYLOAD_PLACEHOLDER,
    PromptError,
    load_prompt,
)
from ipsensitive.moderation.segments import Segment, build_batches

ROOT = Path(__file__).resolve().parents[1]


def test_prompt_file_declares_the_ticket_contract(prompt):
    assert prompt.version == "ip.sensitive.v1"
    assert prompt.batch_size == EXPECTED_BATCH_SIZE == 30
    assert prompt.context_segments == EXPECTED_CONTEXT_SEGMENTS == 1
    assert prompt.flag_types == (
        "third_party_personal",
        "unpublished_business",
        "unlicensed_brand",
        "other",
    )
    assert prompt.reason_max_chars == 40
    assert prompt.quote_max_chars <= 200
    assert (prompt.confidence_min, prompt.confidence_max) == (0.0, 1.0)
    assert prompt.target_only is True
    assert prompt.source_path == ROOT / "prompts" / "ip.sensitive.v1.md"


@pytest.mark.parametrize(
    "anchor",
    ["third_party_personal", "unpublished_business", "unlicensed_brand", "宁可多标", "逐字复制", "[PII:", "confidence", "reason", "不计算"],
)
def test_system_prompt_keeps_each_required_instruction(prompt, anchor):
    assert anchor in prompt.system_text


def _payload_of(prompt, batch) -> dict:
    message = prompt.user_message(batch)
    return json.loads(message[message.index("{") : message.rindex("}") + 1])


def test_prompt_renders_thirty_targets_plus_one_context(prompt):
    segments = [Segment(seq=index, text=f"第{index}段正文。") for index in range(32)]
    batches = build_batches(segments, batch_size=prompt.batch_size, context_segments=prompt.context_segments)
    assert len(batches) == 2
    first = batches[0]
    assert len(first.target_seqs) == 30
    assert first.context_before is None
    assert first.context_after is not None and first.context_after.seq == 30

    message = prompt.user_message(first)
    assert PAYLOAD_PLACEHOLDER not in message
    payload = _payload_of(prompt, first)
    assert payload["prompt_version"] == "ip.sensitive.v1"
    assert payload["batch_index"] == 0
    assert len(payload["target_seqs"]) == 30
    assert payload["target_seqs"][0] == 0 and payload["target_seqs"][-1] == 29
    roles = [segment["role"] for segment in payload["segments"]]
    assert roles.count("target") == 30
    assert roles.count("context_after") == 1
    assert "context_before" not in roles


def test_second_batch_carries_the_previous_segment_as_context(prompt):
    segments = [Segment(seq=index, text=f"第{index}段正文。") for index in range(31)]
    second = build_batches(segments, batch_size=30, context_segments=1)[1]
    assert second.target_seqs == (30,)
    assert second.context_before is not None and second.context_before.seq == 29
    assert second.context_after is None
    roles = {segment["seq"]: segment["role"] for segment in _payload_of(prompt, second)["segments"]}
    assert roles[29] == "context_before"
    assert roles[30] == "target"


def test_messages_put_the_contract_in_the_system_role(prompt):
    batch = build_batches([Segment(seq=0, text="唯一一段。")], batch_size=30)[0]
    messages = prompt.messages(batch)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "宁可多标" in messages[0]["content"]
    assert "唯一一段" in messages[1]["content"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("batch_size: 30", "batch_size: 20"),
        ("context_segments: 1", "context_segments: 2"),
        ("version: ip.sensitive.v1", "version: other.v1"),
        ("宁可多标", "尽量少标"),
        ("逐字复制", "尽量一致"),
    ],
)
def test_prompt_contract_violations_are_rejected(tmp_path, prompt, old, new):
    original = (ROOT / "prompts" / "ip.sensitive.v1.md").read_text(encoding="utf-8")
    assert old in original
    broken = tmp_path / "broken.md"
    broken.write_text(original.replace(old, new), encoding="utf-8")
    with pytest.raises(PromptError):
        load_prompt(broken)


def test_missing_user_placeholder_is_rejected(tmp_path):
    original = (ROOT / "prompts" / "ip.sensitive.v1.md").read_text(encoding="utf-8")
    broken = tmp_path / "broken.md"
    broken.write_text(original.replace(PAYLOAD_PLACEHOLDER, "（无）", 1), encoding="utf-8")
    with pytest.raises(PromptError):
        load_prompt(broken)
