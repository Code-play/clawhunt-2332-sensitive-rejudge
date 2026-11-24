"""Merge contract: one row per fragment, rule wins the type, confidence floor."""

from __future__ import annotations

import pytest

from ipsensitive.moderation.merge import MIN_MODEL_CONFIDENCE, merge_fragments, truncate_reason
from ipsensitive.moderation.rules import RuleHit
from ipsensitive.moderation.schema import ModelFlagHit
from ipsensitive.moderation.segments import Segment


def _model(seq: int, quote: str, flag_type: str = "unpublished_business", confidence: float = 0.9, reason: str = "模型判断", batch: int = 0, targets: tuple[int, ...] | None = None) -> ModelFlagHit:
    return ModelFlagHit(
        seq=seq,
        quote=quote,
        flag_type=flag_type,
        reason=reason,
        confidence=confidence,
        batch_index=batch,
        batch_target_seqs=targets if targets is not None else (seq,),
    )


def _rule(seq: int, quote: str, flag_type: str = "unpublished_business", code: str = "biz.unpublished", reason: str = "规则判断") -> RuleHit:
    return RuleHit(seq=seq, quote=quote, flag_type=flag_type, rule_code=code, reason=reason)


def test_rule_and_model_on_the_same_fragment_become_one_record():
    segment = Segment(seq=0, text="去年公司还没对外公布的产能是每月1800台。")
    outcome = merge_fragments(
        [segment],
        [_rule(0, "还没对外公布")],
        [_model(0, "去年公司还没对外公布的产能是每月1800台")],
        prompt_version="ip.sensitive.v1",
    )
    assert len(outcome.kept) == 1
    candidate = outcome.kept[0]
    assert candidate.detector == "rule+model"
    assert candidate.quote == "去年公司还没对外公布的产能是每月1800台"
    assert candidate.start == 0
    assert candidate.flag_type == "unpublished_business"
    # A deterministic rule hit counts as confidence 1.0, so the merged record keeps it.
    assert candidate.confidence == 1.0
    assert candidate.rule_code == "biz.unpublished"
    assert candidate.prompt_version == "ip.sensitive.v1"
    assert segment.text[candidate.start : candidate.end] == candidate.quote


def test_rule_type_wins_when_the_model_disagrees():
    segment = Segment(seq=0, text="客户手机号 [PII:PHONE] 已经脱敏。")
    outcome = merge_fragments(
        [segment],
        [_rule(0, "[PII:PHONE]", flag_type="third_party_personal", code="mask.pii_placeholder")],
        [_model(0, "客户手机号 [PII:PHONE]", flag_type="other", confidence=0.6)],
    )
    assert len(outcome.kept) == 1
    assert outcome.kept[0].flag_type == "third_party_personal"
    assert outcome.kept[0].detector == "rule+model"


def test_model_only_hit_below_the_floor_is_dropped():
    outcome = merge_fragments(
        [Segment(seq=0, text="今天先聊到这里。")],
        [],
        [_model(0, "今天先聊到这里", flag_type="other", confidence=0.49)],
    )
    assert outcome.kept == ()
    assert outcome.dropped_by_code == {"low_confidence": 1}


@pytest.mark.parametrize("confidence", [MIN_MODEL_CONFIDENCE, 0.5, 0.51])
def test_model_only_hit_at_or_above_the_floor_is_kept(confidence):
    outcome = merge_fragments(
        [Segment(seq=0, text="明年招标的底价。")],
        [],
        [_model(0, "明年招标的底价", confidence=confidence)],
    )
    assert len(outcome.kept) == 1
    assert outcome.kept[0].confidence == confidence
    assert outcome.kept[0].detector == "model"


def test_low_confidence_model_hit_still_merges_with_a_rule_hit():
    # The ticket only drops *model-only* hits below 0.5; an independent rule hit
    # keeps the fragment.
    segment = Segment(seq=0, text="内部会议的结论是砍预算。")
    outcome = merge_fragments(
        [segment],
        [_rule(0, "内部会议")],
        [_model(0, "内部会议的结论", confidence=0.3)],
    )
    assert len(outcome.kept) == 1
    assert outcome.kept[0].detector == "rule+model"
    assert outcome.dropped == ()


@pytest.mark.parametrize("detector", ["rule", "model"])
def test_quote_that_is_not_verbatim_is_dropped(detector):
    segment = Segment(seq=0, text="这是原文。")
    rule_hits = [_rule(0, "不在原文里")] if detector == "rule" else []
    model_flags = [_model(0, "不在原文里")] if detector == "model" else []
    outcome = merge_fragments([segment], rule_hits, model_flags)
    assert outcome.kept == ()
    assert outcome.dropped_by_code == {"quote_not_found": 1}


def test_unknown_segment_is_dropped():
    outcome = merge_fragments([Segment(seq=0, text="正文。")], [], [_model(7, "正文")])
    assert outcome.dropped_by_code == {"unknown_seq": 1}


def test_separate_fragments_stay_separate():
    segment = Segment(seq=0, text="我们的训练营保证收益翻倍，零风险。")
    outcome = merge_fragments(
        [segment],
        [_rule(0, "保证收益", flag_type="other", code="other.income_promise"), _rule(0, "零风险", flag_type="other", code="other.income_promise")],
        [],
    )
    assert len(outcome.kept) == 2
    assert {candidate.quote for candidate in outcome.kept} == {"保证收益", "零风险"}


def test_overlapping_model_quotes_collapse_into_one_fragment():
    segment = Segment(seq=0, text="某竞品公司还没公开的融资消息，是投资人告诉我的。")
    outcome = merge_fragments(
        [segment],
        [],
        [_model(0, "某竞品公司还没公开的融资消息"), _model(0, "还没公开的融资消息")],
    )
    assert len(outcome.kept) == 1
    assert outcome.kept[0].quote == "某竞品公司还没公开的融资消息"


def test_repeated_text_is_flagged_once_at_its_first_occurrence():
    segment = Segment(seq=0, text="内部数据不能写。内部数据也不能引用。")
    outcome = merge_fragments([segment], [], [_model(0, "内部数据")])
    assert len(outcome.kept) == 1
    assert outcome.kept[0].start == 0
    assert outcome.kept[0].end == 4


def test_merged_quote_never_exceeds_the_stored_limit():
    segment = Segment(seq=0, text="啊" * 200 + "内部数据" + "啊" * 50)
    outcome = merge_fragments(
        [segment],
        [_rule(0, "内部数据")],
        [_model(0, "啊" * 200 + "内部数据")],
    )
    assert len(outcome.kept) == 1
    assert len(outcome.kept[0].quote) == 200
    assert outcome.kept[0].quote in segment.text


def test_rule_only_fragment_has_no_prompt_version():
    outcome = merge_fragments(
        [Segment(seq=0, text="内部数据不能写。")],
        [_rule(0, "内部数据")],
        [],
    )
    assert outcome.kept[0].detector == "rule"
    assert outcome.kept[0].prompt_version is None
    assert outcome.kept[0].confidence == 1.0


def test_context_segment_flag_is_kept_and_counted():
    segments = [Segment(seq=0, text="这周先聊到这里。"), Segment(seq=1, text="下一段。")]
    outcome = merge_fragments(
        segments,
        [],
        [_model(0, "这周先聊到这里", flag_type="other", batch=1, targets=(1,))],
    )
    assert len(outcome.kept) == 1
    assert outcome.context_seq_flags == 1


def test_truncate_reason_keeps_the_limit_and_falls_back():
    assert len(truncate_reason("长" * 60)) == 40
    assert truncate_reason("   ") == "未说明理由"


def test_confidence_floor_is_validated():
    with pytest.raises(ValueError):
        merge_fragments([Segment(seq=0, text="x")], [], [], min_model_confidence=1.5)
