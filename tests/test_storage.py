"""Repository and schema behaviour of ``ip_sensitive_flags``."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DataError, IntegrityError

from ipsensitive.models.base import utcnow
from ipsensitive.models.flags import IpSensitiveFlag
from ipsensitive.moderation.merge import Candidate
from ipsensitive.repo import FlagNotFound, InvalidDisposition, SensitiveFlagRepository


def _candidate(seq: int = 0, quote: str = "还没对外公布的产能", *, start: int = 4, flag_type: str = "unpublished_business", detector: str = "model", confidence: float = 0.8) -> Candidate:
    return Candidate(
        seq=seq,
        quote=quote,
        start=start,
        end=start + len(quote),
        flag_type=flag_type,
        detector=detector,
        confidence=confidence,
        reason="未公开产能",
        rule_code=None,
        prompt_version="ip.sensitive.v1" if detector != "rule" else None,
    )


def _insert(repository: SensitiveFlagRepository, tenant, *, content_id=None, candidates=None):
    content = content_id or uuid.uuid4()
    result = repository.insert_candidates(
        candidates or [_candidate()],
        org_id=tenant["org_id"],
        site_id=tenant["site_id"],
        content_id=content,
        run_id=uuid.uuid4(),
    )
    return content, result


def test_new_flags_start_pending_and_are_tenant_scoped(session, tenant):
    repository = SensitiveFlagRepository(session)
    content_id, result = _insert(repository, tenant)
    assert result.written == 1 and result.skipped == 0

    rows, total = repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"])
    assert total == 1
    row = rows[0]
    assert row.disposition == "pending"
    assert row.reviewed_by is None and row.reviewed_at is None
    assert float(row.confidence) == 0.8
    assert str(row.content_id) == str(content_id)
    assert row.quote == "还没对外公布的产能"

    assert repository.list_flags(org_id=uuid.uuid4(), site_id=tenant["site_id"])[1] == 0
    with pytest.raises(FlagNotFound):
        repository.get(row.id, org_id=uuid.uuid4(), site_id=tenant["site_id"])


def test_insertion_skips_the_same_fragment_twice(session, tenant):
    repository = SensitiveFlagRepository(session)
    content_id, first = _insert(repository, tenant)
    _, second = _insert(repository, tenant, content_id=content_id)
    assert first.written == 1
    assert second.written == 0 and second.skipped == 1
    assert session.scalar(select(func.count()).select_from(IpSensitiveFlag)) == 1


def test_the_database_rejects_a_second_row_for_the_same_fragment(session, tenant):
    repository = SensitiveFlagRepository(session)
    content_id, _ = _insert(repository, tenant)
    session.add(
        IpSensitiveFlag(
            org_id=tenant["org_id"],
            site_id=tenant["site_id"],
            content_id=content_id,
            run_id=uuid.uuid4(),
            segment_seq=0,
            quote="还没对外公布的产能",
            quote_start=4,
            quote_end=12,
            flag_type="unpublished_business",
            detector="rule",
            confidence=Decimal("1.000"),
            reason="重复写入",
            disposition="pending",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_soft_deleted_rows_are_hidden_and_can_be_recreated(session, tenant):
    repository = SensitiveFlagRepository(session)
    content_id, _ = _insert(repository, tenant)
    row = repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"])[0][0]

    repository.soft_delete(row.id, org_id=tenant["org_id"], site_id=tenant["site_id"])
    assert repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"])[1] == 0
    with pytest.raises(FlagNotFound):
        repository.get(row.id, org_id=tenant["org_id"], site_id=tenant["site_id"])

    _, again = _insert(repository, tenant, content_id=content_id)
    assert again.written == 1
    assert repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"])[1] == 1


def test_filtering_by_disposition_and_pagination(session, tenant):
    repository = SensitiveFlagRepository(session)
    content_id = uuid.uuid4()
    candidates = [_candidate(seq=index, quote=f"内部数据{index}号", start=0) for index in range(5)]
    _, result = _insert(repository, tenant, content_id=content_id, candidates=candidates)
    assert result.written == 5

    first = repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"], limit=2, offset=0)
    second = repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"], limit=2, offset=2)
    assert first[1] == 5 and len(first[0]) == 2 and len(second[0]) == 2
    assert {row.id for row in first[0]}.isdisjoint({row.id for row in second[0]})

    row = first[0][0]
    repository.set_disposition(
        row.id,
        org_id=tenant["org_id"],
        site_id=tenant["site_id"],
        disposition="accepted",
        reviewed_by=uuid.uuid4(),
        review_note="确认敏感",
    )
    pending = repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"], disposition="pending")
    accepted = repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"], disposition="accepted")
    assert pending[1] == 4 and accepted[1] == 1
    assert accepted[0][0].review_note == "确认敏感"


def test_staff_disposition_rules(session, tenant):
    repository = SensitiveFlagRepository(session)
    _insert(repository, tenant)
    row = repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"])[0][0]
    staff = uuid.uuid4()

    reviewed = repository.set_disposition(
        row.id,
        org_id=tenant["org_id"],
        site_id=tenant["site_id"],
        disposition="dismissed",
        reviewed_by=staff,
        review_note=None,
    )
    assert reviewed.disposition == "dismissed"
    assert reviewed.reviewed_by == staff
    assert reviewed.reviewed_at is not None

    with pytest.raises(InvalidDisposition):
        repository.set_disposition(
            row.id,
            org_id=tenant["org_id"],
            site_id=tenant["site_id"],
            disposition="pending",
            reviewed_by=staff,
        )
    with pytest.raises(ValueError):
        repository.set_disposition(
            row.id,
            org_id=tenant["org_id"],
            site_id=tenant["site_id"],
            disposition="accepted",
            reviewed_by=staff,
            review_note="x" * 501,
        )


def test_database_checks_reject_invalid_rows(session, tenant):
    repository = SensitiveFlagRepository(session)
    content_id, _ = _insert(repository, tenant)
    base = dict(
        org_id=tenant["org_id"],
        site_id=tenant["site_id"],
        content_id=content_id,
        run_id=uuid.uuid4(),
        segment_seq=0,
        quote="片段",
        quote_start=0,
        quote_end=2,
        flag_type="other",
        detector="model",
        confidence=Decimal("0.800"),
        reason="理由",
        disposition="pending",
    )
    broken_cases = [
        {"detector": "mixed"},  # detector not in the allowed set
        {"flag_type": "gossip"},  # flag type not in the allowed set
        {"confidence": Decimal("1.400")},  # outside 0..1
        {"reason": "太" * 41},  # longer than 40 characters
        {"quote_start": 5, "quote_end": 4},  # invalid span
        {"disposition": "reviewed"},  # not a disposition
        {"disposition": "accepted"},  # accepted without reviewer (review state check)
    ]
    for overrides in broken_cases:
        row = IpSensitiveFlag(**{**base, **overrides})
        session.add(row)
        with pytest.raises((IntegrityError, DataError)):
            session.flush()
        session.rollback()


def test_review_timestamp_comes_from_the_clock(session, tenant):
    repository = SensitiveFlagRepository(session)
    _insert(repository, tenant)
    row = repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"])[0][0]
    before = utcnow()
    reviewed = repository.set_disposition(
        row.id,
        org_id=tenant["org_id"],
        site_id=tenant["site_id"],
        disposition="accepted",
        reviewed_by=uuid.uuid4(),
    )
    assert reviewed.reviewed_at is not None and reviewed.reviewed_at >= before.replace(microsecond=0)


def test_list_limit_is_validated(session, tenant):
    repository = SensitiveFlagRepository(session)
    with pytest.raises(ValueError):
        repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"], limit=0)
    with pytest.raises(ValueError):
        repository.list_flags(org_id=tenant["org_id"], site_id=tenant["site_id"], disposition="unknown")
