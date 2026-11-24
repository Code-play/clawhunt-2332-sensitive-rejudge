"""Acceptance criterion 3: the same fragment hit by rules and model is one row."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from ipsensitive.models.flags import IpSensitiveFlag
from ipsensitive.moderation.pipeline import SensitiveReviewer, build_request
from ipsensitive.moderation.segments import Segment

TEXT = "去年公司还没对外公布的产能是每月1800台。"


def _reviewer(session, prompt, settings, make_client):
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {
                        "seq": 0,
                        "quote": TEXT,
                        "flag_type": "unpublished_business",
                        "reason": "未公开产能",
                        "confidence": 0.9,
                    }
                ]
            }
        }
    )
    return SensitiveReviewer(session=session, model_client=client, prompt=prompt, settings=settings)


def _request(tenant, content_id):
    return build_request(
        org_id=tenant["org_id"],
        site_id=tenant["site_id"],
        content_id=content_id,
        segments=[Segment(seq=0, text=TEXT)],
    )


def _count(session) -> int:
    return int(session.scalar(select(func.count()).select_from(IpSensitiveFlag)) or 0)


def test_rule_and_model_hit_on_the_same_fragment_writes_exactly_one_row(session, prompt, settings, make_client, tenant):
    content_id = uuid.uuid4()
    result = _reviewer(session, prompt, settings, make_client).run(_request(tenant, content_id))

    rows = list(session.scalars(select(IpSensitiveFlag)).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.detector == "rule+model"
    assert row.flag_type == "unpublished_business"
    assert row.detector in {"rule", "model", "rule+model"}
    assert row.quote == TEXT
    assert TEXT[row.quote_start : row.quote_end] == row.quote
    assert str(row.content_id) == str(content_id)
    assert result.insert.written == 1
    assert result.kept == 1


def test_rerunning_the_pipeline_does_not_duplicate_the_row(session, prompt, settings, make_client, tenant):
    reviewer = _reviewer(session, prompt, settings, make_client)
    content_id = uuid.uuid4()
    first = reviewer.run(_request(tenant, content_id))
    second = reviewer.run(_request(tenant, content_id))
    assert first.insert.written == 1
    assert second.insert.written == 0
    assert second.insert.skipped == 1
    assert _count(session) == 1


def test_rule_and_model_hit_with_different_spans_still_writes_one_row(session, prompt, settings, make_client, tenant):
    # The model quotes a wider span around the rule hit; the merge writes the
    # union as a single rule+model row instead of two overlapping rows.
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {
                        "seq": 0,
                        "quote": "去年公司还没对外公布的产能",
                        "flag_type": "unpublished_business",
                        "reason": "未公开产能",
                        "confidence": 0.7,
                    }
                ]
            }
        }
    )
    reviewer = SensitiveReviewer(session=session, model_client=client, prompt=prompt, settings=settings)
    reviewer.run(_request(tenant, uuid.uuid4()))
    rows = list(session.scalars(select(IpSensitiveFlag)).all())
    assert len(rows) == 1
    assert rows[0].detector == "rule+model"
    assert rows[0].quote == "去年公司还没对外公布的产能"


def test_detector_values_are_recorded_for_pure_rule_and_pure_model_fragments(session, prompt, settings, make_client, tenant):
    body = "去年公司还没对外公布的产能是每月1800台，这个数字我从没在公开场合讲过。"
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {
                        "seq": 0,
                        "quote": "这个数字我从没在公开场合讲过",
                        "flag_type": "unpublished_business",
                        "reason": "未公开信息",
                        "confidence": 0.8,
                    }
                ]
            }
        }
    )
    reviewer = SensitiveReviewer(session=session, model_client=client, prompt=prompt, settings=settings)
    reviewer.run(
        build_request(
            org_id=tenant["org_id"],
            site_id=tenant["site_id"],
            content_id=uuid.uuid4(),
            segments=[Segment(seq=0, text=body)],
        )
    )
    rows = {row.detector for row in session.scalars(select(IpSensitiveFlag)).all()}
    assert rows == {"rule", "model"}
