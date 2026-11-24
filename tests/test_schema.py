"""Validation of raw model answers: one bad entry never ruins a whole batch."""

from __future__ import annotations

import json

import pytest

from ipsensitive.moderation.schema import (
    FLAG_TYPES,
    ModelFlag,
    parse_model_output,
)


def _answer(flags: list[dict]) -> str:
    return json.dumps({"flags": flags}, ensure_ascii=False)


def _flag(**overrides) -> dict:
    base = {
        "seq": 3,
        "quote": "还没对外公布的产能",
        "flag_type": "unpublished_business",
        "reason": "未公开产能",
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def test_plain_json_answer_is_parsed():
    parsed = parse_model_output(_answer([_flag()]))
    assert parsed.rejected == ()
    assert len(parsed.flags) == 1
    flag = parsed.flags[0]
    assert flag.seq == 3 and flag.confidence == 0.9
    assert isinstance(flag, ModelFlag)


def test_code_fenced_answer_is_parsed():
    raw = "```json\n" + _answer([_flag()]) + "\n```"
    assert len(parse_model_output(raw).flags) == 1


def test_answer_with_leading_prose_is_parsed():
    raw = "好的，下面是结果：\n" + _answer([_flag()]) + "\n希望有帮助。"
    parsed = parse_model_output(raw)
    assert len(parsed.flags) == 1
    assert parsed.rejected == ()


def test_empty_answer_is_reported():
    parsed = parse_model_output("   ")
    assert parsed.flags == ()
    assert [item.code for item in parsed.rejected] == ["empty_response"]


def test_non_json_answer_is_reported():
    parsed = parse_model_output("抱歉，我无法判断。")
    assert [item.code for item in parsed.rejected] == ["invalid_json"]


def test_missing_flags_key_is_reported():
    parsed = parse_model_output('{"result": []}')
    assert [item.code for item in parsed.rejected] == ["missing_flags"]


def test_flags_not_a_list_is_reported():
    parsed = parse_model_output('{"flags": {}}')
    assert [item.code for item in parsed.rejected] == ["flags_not_a_list"]


def test_one_bad_entry_does_not_discard_the_others():
    parsed = parse_model_output(_answer([_flag(), {"seq": 1, "quote": "x", "flag_type": "gossip", "reason": "y", "confidence": 0.5}, _flag(seq=4)]))
    assert [flag.seq for flag in parsed.flags] == [3, 4]
    assert [item.code for item in parsed.rejected] == ["bad_flag_type"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"flag_type": "unknown_type"}, "bad_flag_type"),
        ({"reason": "太" * 41}, "reason_too_long"),
        ({"confidence": 1.4}, "confidence_out_of_range"),
        ({"confidence": -0.1}, "confidence_out_of_range"),
        ({"quote": ""}, "bad_quote"),
        ({"quote": "x" * 201}, "bad_quote"),
        ({"seq": -1}, "bad_seq"),
        ({"seq": "abc"}, "bad_seq"),
        ({"extra": "field"}, "unexpected_field"),
    ],
)
def test_invalid_entries_are_rejected_with_a_code(overrides, code):
    parsed = parse_model_output(_answer([_flag(**overrides)]))
    assert parsed.flags == ()
    assert [item.code for item in parsed.rejected] == [code]


def test_entry_without_a_required_field_is_rejected():
    entry = _flag()
    entry.pop("confidence")
    parsed = parse_model_output(_answer([entry]))
    assert parsed.flags == ()
    assert [item.code for item in parsed.rejected] == ["missing_field"]


def test_non_object_entry_is_rejected():
    parsed = parse_model_output(_answer(["not an object"]))
    assert [item.code for item in parsed.rejected] == ["flag_not_an_object"]


def test_prompt_contract_is_used_for_validation():
    # The prompt declares the reason limit; passing it through enforces a stricter
    # limit than the database column before anything is written.
    parsed_strict = parse_model_output(_answer([_flag()]), reason_max_chars=3)
    assert [item.code for item in parsed_strict.rejected] == ["reason_too_long"]


def test_all_ticket_flag_types_are_accepted():
    flags = [_flag(flag_type=flag_type) for flag_type in FLAG_TYPES]
    parsed = parse_model_output(_answer(flags))
    assert {flag.flag_type for flag in parsed.flags} == set(FLAG_TYPES)


def test_too_many_flags_is_rejected():
    parsed = parse_model_output(json.dumps({"flags": [_flag() for _ in range(301)]}))
    assert [item.code for item in parsed.rejected] == ["too_many_flags"]
