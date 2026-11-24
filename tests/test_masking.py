"""The unified masking gate: identifiers never reach the model, spans still map back."""

from __future__ import annotations

import pytest

from ipsensitive.moderation.masking import mask_segments, mask_text
from ipsensitive.moderation.segments import Segment


def test_masks_each_direct_identifier_with_a_typed_placeholder():
    masked = mask_text("联系 13812345678，邮箱 zhao@example.com，身份证 330106199001011234，卡号 6222021234567890123。")
    assert "13812345678" not in masked.text
    assert "zhao@example.com" not in masked.text
    assert "330106199001011234" not in masked.text
    assert "6222021234567890123" not in masked.text
    assert "[PII:PHONE]" in masked.text
    assert "[PII:EMAIL]" in masked.text
    assert "[PII:ID]" in masked.text
    assert "[PII:BANK]" in masked.text


def test_roster_names_are_masked_when_supplied():
    masked = mask_text("周海涛上周离职了。", roster=["周海涛"])
    assert masked.text == "[PII:NAME]上周离职了。"


def test_plain_text_is_untouched():
    masked = mask_text("去年产能是每月1800台，没有联系方式。")
    assert masked.text == "去年产能是每月1800台，没有联系方式。"
    assert masked.replacements == ()


def test_masking_is_idempotent():
    once = mask_text("电话 13812345678。")
    twice = mask_text(once.text)
    assert twice.text == once.text


def test_placeholder_maps_back_to_the_raw_span():
    raw = "手机号 13812345678，他在A厂上班。"
    masked = mask_text(raw)
    start = masked.text.index("[PII:PHONE]")
    raw_span = masked.raw_span(start, start + len("[PII:PHONE]"))
    assert raw_span is not None
    assert raw[raw_span[0] : raw_span[1]] == "13812345678"


def test_span_that_cuts_a_placeholder_has_no_raw_counterpart():
    masked = mask_text("手机号 13812345678。")
    start = masked.text.index("[PII:PHONE]")
    assert masked.raw_span(start + 1, start + 5) is None


def test_plain_span_maps_back_with_the_offset_shift():
    raw = "手机号 13812345678，他在A厂上班。"
    masked = mask_text(raw)
    start = masked.text.index("他在")
    raw_span = masked.raw_span(start, start + 2)
    assert raw_span is not None
    assert raw[raw_span[0] : raw_span[1]] == "他在"


def test_mask_segments_rewrites_every_segment_and_counts_replacements():
    segments = [
        Segment(seq=0, text="邮箱 hello@example.com。"),
        Segment(seq=1, text="电话 13900001111 和 13700002222。"),
        Segment(seq=2, text="没有敏感信息。"),
    ]
    masked = mask_segments(segments)
    assert [segment.text for segment in masked.segments] == [
        "邮箱 [PII:EMAIL]。",
        "电话 [PII:PHONE] 和 [PII:PHONE]。",
        "没有敏感信息。",
    ]
    assert masked.replacements == 3
    assert masked.per_segment[1].replacements[0].kind == "PHONE"


def test_no_eleven_digit_run_survives_masking():
    masked = mask_segments([Segment(seq=0, text="甲乙 13900001111 丙丁 13700002222。")])
    import re

    assert re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", masked.segments[0].text) is None


@pytest.mark.parametrize(("kind", "raw"), [("PHONE", "13800009999"), ("ID", "330106199001011234")])
def test_placeholder_kind_is_reported(kind, raw):
    masked = mask_text(f"号码 {raw}。")
    assert masked.replacements[0].kind == kind
    assert masked.replacements[0].raw_start == 3
