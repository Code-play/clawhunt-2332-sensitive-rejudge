"""Counterexamples: the paths that must not create or corrupt flags."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, inspect, select

from ipsensitive.db import make_engine
from ipsensitive.models.flags import IpSensitiveFlag
from ipsensitive.moderation.model_client import ModelError
from ipsensitive.moderation.pipeline import SensitiveReviewer, build_request
from ipsensitive.moderation.schema import ModelFlag
from ipsensitive.moderation.segments import Segment

TEXT = "去年公司还没对外公布的产能是每月1800台。"


def _count(session) -> int:
    return int(session.scalar(select(func.count()).select_from(IpSensitiveFlag)) or 0)


def _reviewer(session, prompt, settings, client) -> SensitiveReviewer:
    return SensitiveReviewer(session=session, model_client=client, prompt=prompt, settings=settings)


def _request(tenant, *, text: str = TEXT, segments=None):
    return build_request(
        org_id=tenant["org_id"],
        site_id=tenant["site_id"],
        content_id=uuid.uuid4(),
        segments=segments if segments is not None else [Segment(seq=0, text=text)],
    )


def test_model_flag_on_a_missing_segment_is_dropped(session, prompt, settings, make_client, tenant):
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {
                        "seq": 99,
                        "quote": "每月1800台",
                        "flag_type": "unpublished_business",
                        "reason": "不存在的分段",
                        "confidence": 0.9,
                    }
                ]
            }
        }
    )
    result = _reviewer(session, prompt, settings, client).run(_request(tenant, text="去年产能是每月1800台。"))
    assert result.dropped_by_code == {"unknown_seq": 1}
    assert _count(session) == 0


def test_rejected_model_entries_are_counted_not_written(session, prompt, settings, make_client, tenant):
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {"seq": 0, "quote": "还没对外公布", "flag_type": "gossip", "reason": "未知类型", "confidence": 0.9},
                    {"seq": 0, "quote": "还没对外公布", "flag_type": "unpublished_business", "reason": "太" * 41, "confidence": 0.9},
                    {"seq": 0, "quote": "还没对外公布", "flag_type": "unpublished_business", "reason": "合法条目", "confidence": 0.9},
                ]
            }
        }
    )
    result = _reviewer(session, prompt, settings, client).run(_request(tenant))
    assert result.model_rejected == 2
    assert result.model_flags == 1
    assert result.batches[0].rejects[0]["code"] == "bad_flag_type"
    assert result.batches[0].rejects[1]["code"] == "reason_too_long"
    assert _count(session) == 1


def test_non_json_model_answer_keeps_the_rule_flags(session, prompt, settings, make_client, tenant):
    client = make_client({"*:0": "抱歉，我无法判断这段内容。", })
    result = _reviewer(session, prompt, settings, client).run(_request(tenant))
    assert result.model_rejected == 1
    assert result.model_flags == 0
    assert result.model_status == "ok"
    rows = list(session.scalars(select(IpSensitiveFlag)).all())
    assert [row.detector for row in rows] == ["rule"]


def test_confidence_floor_boundary(session, prompt, settings, make_client, tenant):
    body = "去年公司还没对外公布的产能是每月1800台，这个数字我从没在公开场合讲过。"
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {"seq": 0, "quote": "还没对外公布", "flag_type": "unpublished_business", "reason": "与规则重合", "confidence": 0.49},
                    {"seq": 0, "quote": "这个数字我从没在公开场合讲过", "flag_type": "unpublished_business", "reason": "边界置信", "confidence": 0.5},
                    {"seq": 0, "quote": "每月1800台", "flag_type": "unpublished_business", "reason": "纯模型低置信", "confidence": 0.49},
                ]
            }
        }
    )
    result = _reviewer(session, prompt, settings, client).run(_request(tenant, text=body))
    # The 0.49 hit that overlaps a rule hit survives as rule+model (the ticket
    # only drops model-only hits below 0.5); the 0.5 hit is kept as model-only;
    # the 0.49 model-only hit is dropped.
    assert result.dropped_by_code == {"low_confidence": 1}
    detectors = {row.detector for row in session.scalars(select(IpSensitiveFlag)).all()}
    assert detectors == {"rule+model", "model"}
    stored = {row.quote: row.confidence for row in session.scalars(select(IpSensitiveFlag)).all()}
    assert stored["这个数字我从没在公开场合讲过"] == Decimal("0.500")


def test_model_flag_on_a_context_segment_is_kept_and_counted(session, prompt, settings, make_client, tenant):
    # 61 segments -> three batches; a flag returned for a context segment of the
    # batch it came from is kept (recall-first) and counted as a diagnostic.
    segments = [Segment(seq=index, text=f"第{index}段正文。") for index in range(61)]
    segments[0] = Segment(seq=0, text="内部数据不能写。")
    segments[59] = Segment(seq=59, text="内部数据也不能引用。")
    client = make_client(
        {
            "*:0": {"flags": []},
            "*:1": {"flags": []},
            "*:2": {
                "flags": [
                    {"seq": 59, "quote": "内部数据也不能引用", "flag_type": "unpublished_business", "reason": "越界上下文", "confidence": 0.9}
                ]
            },
        }
    )
    result = _reviewer(session, prompt, settings, client).run(_request(tenant, segments=segments))
    assert result.context_seq_flags == 1
    assert result.kept >= 1


def test_model_failure_with_fail_policy_raises_and_writes_nothing(session, prompt, settings, make_client, tenant):
    client = make_client({})
    with pytest.raises(ModelError):
        _reviewer(session, prompt, settings, client).run(_request(tenant))
    assert _count(session) == 0


def test_model_failure_with_rules_only_policy_is_degraded_but_writes_rules(session, prompt, settings, make_client, tenant):
    from dataclasses import replace

    degraded = replace(settings, on_model_error="rules_only")
    client = make_client({})
    result = _reviewer(session, prompt, degraded, client).run(_request(tenant))
    assert result.degraded is True
    assert result.model_status == "unavailable"
    assert result.batches[0].error is not None
    rows = list(session.scalars(select(IpSensitiveFlag)).all())
    assert [row.detector for row in rows] == ["rule"]


def test_duplicate_and_missing_segments_are_rejected_before_any_model_call(prompt, settings, make_client, tenant):
    client = make_client({})
    with pytest.raises(ValueError):
        build_request(
            org_id=tenant["org_id"],
            site_id=tenant["site_id"],
            content_id=uuid.uuid4(),
            segments=[{"seq": 0, "text": "a"}, {"seq": 0, "text": "b"}],
        )
    with pytest.raises(ValueError):
        build_request(
            org_id=tenant["org_id"],
            site_id=tenant["site_id"],
            content_id=uuid.uuid4(),
            segments=[],
        )


def test_only_masked_quotes_are_stored(session, prompt, settings, make_client, tenant):
    raw = "A 厂采购经理姓吴，手机号 13812345678，他私下给我报价。"
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {
                        "seq": 0,
                        "quote": "采购经理姓吴，手机号 [PII:PHONE]",
                        "flag_type": "third_party_personal",
                        "reason": "第三方联系方式",
                        "confidence": 0.9,
                    }
                ]
            }
        }
    )
    result = _reviewer(session, prompt, settings, client).run(_request(tenant, text=raw))
    assert result.mask_replacements == 1
    row = session.scalars(select(IpSensitiveFlag)).one()
    assert row.quote == "采购经理姓吴，手机号 [PII:PHONE]"
    assert "13812345678" not in row.quote
    assert re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", row.quote) is None


def test_ai_stage_has_no_monetary_output(settings):
    # "AI 不计算金额": neither the stored table nor the model contract carries money.
    engine = make_engine(settings.database_url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("ip_sensitive_flags")}
    finally:
        engine.dispose()
    assert not any(term in name for name in columns for term in ("amount", "price", "cents", "currency"))
    assert not any(term in field for field in ModelFlag.model_fields for term in ("amount", "price", "cents"))


def test_confidence_is_stored_with_three_decimals(session, prompt, settings, make_client, tenant):
    body = "这个数字我从没在公开场合讲过。"
    client = make_client(
        {
            "*:0": {
                "flags": [
                    {
                        "seq": 0,
                        "quote": "这个数字我从没在公开场合讲过",
                        "flag_type": "unpublished_business",
                        "reason": "测试精度",
                        "confidence": 0.8766,
                    }
                ]
            }
        }
    )
    _reviewer(session, prompt, settings, client).run(_request(tenant, text=body))
    row = session.scalars(select(IpSensitiveFlag)).one()
    assert row.confidence == Decimal("0.877")
