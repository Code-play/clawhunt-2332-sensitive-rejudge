"""Acceptance criterion 2: a quote that is not in the source is discarded.

The drafts used here contain no rule-stage trigger, so a row can only come from
the model flag under test.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from ipsensitive.models.flags import IpSensitiveFlag
from ipsensitive.moderation.pipeline import SensitiveReviewer, build_request
from ipsensitive.moderation.rules import RuleHit
from ipsensitive.moderation.segments import Segment

TEXT = "我们去年做了三轮渠道试点，效果一般。"


def _count(session) -> int:
    return int(session.scalar(select(func.count()).select_from(IpSensitiveFlag)) or 0)


def _run(session, prompt, settings, make_client, tenant, *, text: str = TEXT, model_quote: str | None, rule_hits=None, segments=None):
    flags = [] if model_quote is None else [
        {
            "seq": 0,
            "quote": model_quote,
            "flag_type": "unpublished_business",
            "reason": "未公开经营信息",
            "confidence": 0.9,
        }
    ]
    client = make_client({"*:0": {"flags": flags}})
    reviewer = SensitiveReviewer(session=session, model_client=client, prompt=prompt, settings=settings)
    return reviewer.run(
        build_request(
            org_id=tenant["org_id"],
            site_id=tenant["site_id"],
            content_id=uuid.uuid4(),
            segments=segments if segments is not None else [Segment(seq=0, text=text)],
            rule_hits=rule_hits,
        )
    )


def test_paraphrased_model_quote_is_discarded(session, prompt, settings, make_client, tenant):
    result = _run(session, prompt, settings, make_client, tenant, model_quote="公司做了四轮渠道试点")
    assert _count(session) == 0
    assert result.dropped_by_code == {"quote_not_found": 1}
    assert result.kept == 0


def test_quote_with_different_whitespace_is_not_accepted(session, prompt, settings, make_client, tenant):
    result = _run(session, prompt, settings, make_client, tenant, model_quote="我们去年做了三轮渠道 试点")
    assert _count(session) == 0
    assert result.dropped_by_code == {"quote_not_found": 1}


def test_quote_from_another_segment_is_discarded(session, prompt, settings, make_client, tenant):
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {
                        "seq": 1,
                        "quote": TEXT,
                        "flag_type": "unpublished_business",
                        "reason": "分段错位",
                        "confidence": 0.9,
                    }
                ]
            }
        }
    )
    reviewer = SensitiveReviewer(session=session, model_client=client, prompt=prompt, settings=settings)
    result = reviewer.run(
        build_request(
            org_id=tenant["org_id"],
            site_id=tenant["site_id"],
            content_id=uuid.uuid4(),
            segments=[Segment(seq=0, text=TEXT), Segment(seq=1, text="另一段正文。")],
        )
    )
    assert _count(session) == 0
    assert result.dropped_by_code == {"quote_not_found": 1}


def test_a_verbatim_quote_is_kept_as_the_control_case(session, prompt, settings, make_client, tenant):
    result = _run(session, prompt, settings, make_client, tenant, model_quote="三轮渠道试点")
    assert _count(session) == 1
    assert result.dropped_by_code == {}
    row = session.scalars(select(IpSensitiveFlag)).one()
    assert row.quote == "三轮渠道试点"
    assert row.detector == "model"


def test_upstream_rule_hit_with_a_bad_quote_is_discarded_too(session, prompt, settings, make_client, tenant):
    result = _run(
        session,
        prompt,
        settings,
        make_client,
        tenant,
        model_quote=None,
        rule_hits=[RuleHit(seq=0, quote="原文里没有这句", flag_type="unpublished_business", rule_code="biz.unpublished", reason="规则")],
    )
    assert _count(session) == 0
    assert result.dropped_by_code == {"quote_not_found": 1}
