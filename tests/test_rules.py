"""The rule stage stub: deterministic hits the model then re-judges."""

from __future__ import annotations

import pytest

from ipsensitive.moderation.rules import RULES, RegexRuleDetector, RuleHit
from ipsensitive.moderation.segments import Segment


def test_detector_finds_each_rule_family():
    segments = [
        Segment(seq=0, text="客户手机号 [PII:PHONE] 已经脱敏。"),
        Segment(seq=1, text="去年还没对外公布的产能是每月1800台。"),
        Segment(seq=2, text="海报未经授权就用了对方的商标。"),
        Segment(seq=3, text="我们的服务百分之百保证有效。"),
        Segment(seq=4, text="这个项目稳赚，零风险。"),
    ]
    hits = RegexRuleDetector().detect(segments)
    by_seq = {}
    for hit in hits:
        by_seq.setdefault(hit.seq, set()).add(hit.rule_code)
    assert "mask.pii_placeholder" in by_seq[0]
    assert "biz.unpublished" in by_seq[1]
    assert "brand.unlicensed" in by_seq[2]
    assert {"other.absolute"} & by_seq[3]
    assert {"other.income_promise"} & by_seq[4]


def test_hit_types_come_from_the_rule_specification():
    hits = RegexRuleDetector().detect([Segment(seq=0, text="内部会议的结论是砍预算。")])
    assert [hit.flag_type for hit in hits] == ["unpublished_business"]
    assert hits[0].quote == "内部会议"


def test_reasons_fit_the_stored_limit():
    assert all(len(rule.reason) <= 40 for rule in RULES)


def test_rule_hit_validates_its_contract():
    with pytest.raises(ValueError):
        RuleHit(seq=0, quote="", flag_type="other", rule_code="x")
    with pytest.raises(ValueError):
        RuleHit(seq=0, quote="x", flag_type="gossip", rule_code="x")
    with pytest.raises(ValueError):
        RuleHit(seq=0, quote="x", flag_type="other", rule_code="x", reason="太" * 41)


def test_rule_codes_are_unique():
    codes = [rule.code for rule in RULES]
    assert len(codes) == len(set(codes))
