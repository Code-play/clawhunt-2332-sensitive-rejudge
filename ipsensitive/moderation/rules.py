"""The rule stage (stand-in for the upstream detector).

The ticket places the model *after* rule detection: rules run first, the model
adds what the rules missed and re-judges their hits.  The real rule engine is
another ticket; per the platform convention this package ships a deterministic
stub with the same contract, and the integration point is documented in
README.md.  Upstream hits can also be supplied directly (API payload, tests).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .schema import FLAG_TYPES
from .segments import Segment

REASON_MAX_CHARS = 40
QUOTE_MAX_CHARS = 200


@dataclass(frozen=True)
class RuleSpec:
    code: str
    flag_type: str
    pattern: re.Pattern[str]
    reason: str


#: Demonstration rules.  Deliberately small: they stand in for the upstream rule
#: engine and exist so the merge (rule + model -> one row) can be exercised.
RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        code="mask.pii_placeholder",
        flag_type="third_party_personal",
        pattern=re.compile(r"\[PII:(?:PHONE|ID|EMAIL|BANK|NAME)\]"),
        reason="脱敏占位符对应第三方个人信息",
    ),
    RuleSpec(
        code="biz.unpublished",
        flag_type="unpublished_business",
        pattern=re.compile(r"未公开|未对外公布|还没对外公布|还没对外说|内部数据|内部产能|内部价格|内部会议|还没公开"),
        reason="未公开的经营信息",
    ),
    RuleSpec(
        code="brand.unlicensed",
        flag_type="unlicensed_brand",
        pattern=re.compile(r"仿款|同款|照抄|未授权|盗版|未经授权|照着[^。；;]{0,6}做"),
        reason="疑似未经授权使用品牌或他人作品",
    ),
    RuleSpec(
        code="other.absolute",
        flag_type="other",
        pattern=re.compile(r"第一品牌|百分之百|绝对安全|保证有效"),
        reason="绝对化用语",
    ),
    RuleSpec(
        code="other.income_promise",
        flag_type="other",
        pattern=re.compile(r"稳赚|包回本|保证收益|零风险|躺赚"),
        reason="承诺收益",
    ),
)


@dataclass(frozen=True)
class RuleHit:
    """One upstream rule detection, expressed on the masked text."""

    seq: int
    quote: str
    flag_type: str
    rule_code: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 0:
            raise ValueError(f"rule hit seq must be a non-negative int, got {self.seq!r}")
        if not isinstance(self.quote, str) or not (1 <= len(self.quote) <= QUOTE_MAX_CHARS):
            raise ValueError("rule hit quote must be 1..200 characters")
        if self.flag_type not in FLAG_TYPES:
            raise ValueError(f"rule hit flag_type must be one of {FLAG_TYPES}, got {self.flag_type!r}")
        if not isinstance(self.rule_code, str) or not (1 <= len(self.rule_code) <= 64):
            raise ValueError("rule hit rule_code must be 1..64 characters")
        if len(self.reason) > REASON_MAX_CHARS:
            raise ValueError(f"rule hit reason must be <= {REASON_MAX_CHARS} characters")


@runtime_checkable
class RuleSource(Protocol):
    """Contract of the upstream rule detector (interface stub in this ticket)."""

    def detect(self, segments: Sequence[Segment]) -> list[RuleHit]:
        ...


class RegexRuleDetector:
    """Deterministic stand-in for the upstream rule stage."""

    def __init__(self, rules: Sequence[RuleSpec] = RULES) -> None:
        self.rules = tuple(rules)

    def detect(self, segments: Sequence[Segment]) -> list[RuleHit]:
        hits: list[RuleHit] = []
        for segment in segments:
            for rule in self.rules:
                for match in rule.pattern.finditer(segment.text):
                    hits.append(
                        RuleHit(
                            seq=segment.seq,
                            quote=match.group(0),
                            flag_type=rule.flag_type,
                            rule_code=rule.code,
                            reason=rule.reason,
                        )
                    )
        return hits


__all__ = ["RULES", "RegexRuleDetector", "RuleHit", "RuleSource", "RuleSpec"]
