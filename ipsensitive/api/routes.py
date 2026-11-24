"""Routes: run a re-review and work the review queue.

Everything is tenant-scoped.  A cross-tenant id is always a plain 404 so the API
does not reveal whether a flag exists in another organisation or site.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ..config import Settings
from ..moderation.model_client import ModelClient, ModelError
from ..moderation.pipeline import SensitiveReviewer, build_request
from ..moderation.prompt import PromptSpec
from ..moderation.segments import Segment
from ..repo import FlagNotFound, InvalidDisposition, SensitiveFlagRepository
from .deps import get_model_client_dep, get_prompt_dep, get_session, get_settings_dep
from .schemas import (
    BatchReportOut,
    Disposition,
    DispositionIn,
    FlagOut,
    FlagPage,
    ReviewIn,
    ReviewOut,
)

router = APIRouter(prefix="/v1", tags=["sensitive-review"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
PromptDep = Annotated[PromptSpec, Depends(get_prompt_dep)]
ModelDep = Annotated[ModelClient, Depends(get_model_client_dep)]


@router.post(
    "/ip-drafts/{content_id}/sensitive-review",
    response_model=ReviewOut,
    status_code=201,
    summary="Re-review one draft: mask, rule stage, model, merge, write pending flags",
)
def run_review(
    content_id: uuid.UUID,
    payload: ReviewIn,
    session: SessionDep,
    settings: SettingsDep,
    prompt: PromptDep,
    model_client: ModelDep,
) -> ReviewOut:
    try:
        request_object = build_request(
            org_id=payload.org_id,
            site_id=payload.site_id,
            content_id=content_id,
            segments=[Segment(seq=item.seq, text=item.text) for item in payload.segments],
            rule_hits=[item.to_hit() for item in payload.rule_hits] if payload.rule_hits is not None else None,
            created_by=payload.created_by,
            roster=tuple(payload.roster),
            document_key=str(content_id),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    reviewer = SensitiveReviewer(
        session=session,
        model_client=model_client,
        prompt=prompt,
        settings=settings,
    )
    try:
        result = reviewer.run(request_object)
    except ModelError as error:
        session.rollback()
        raise HTTPException(status_code=503, detail=f"model unavailable: {error}") from error
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error

    rows = SensitiveFlagRepository(session).flags_of_run(
        run_id=result.run_id, org_id=payload.org_id, site_id=payload.site_id
    )
    return ReviewOut(
        run_id=result.run_id,
        content_id=result.content_id,
        prompt_version=result.prompt_version,
        model_id=result.model_id,
        model_status=result.model_status,
        degraded=result.degraded,
        mask_replacements=result.mask_replacements,
        batches=[
            BatchReportOut(
                index=batch.index,
                target_seqs=list(batch.target_seqs),
                accepted=batch.accepted,
                rejected=batch.rejected,
                rejects=list(batch.rejects),
                error=batch.error,
            )
            for batch in result.batches
        ],
        rule_hits=result.rule_hits,
        model_flags=result.model_flags,
        model_rejected=result.model_rejected,
        kept=result.kept,
        dropped_by_code=result.dropped_by_code,
        context_seq_flags=result.context_seq_flags,
        written=result.insert.written,
        skipped_duplicates=result.insert.skipped,
        flags=[FlagOut.from_row(row) for row in rows],
    )


@router.get(
    "/ip-drafts/{content_id}/sensitive-flags",
    response_model=FlagPage,
    summary="Review queue of one draft (masked quotes only)",
)
def list_flags(
    content_id: uuid.UUID,
    org_id: uuid.UUID,
    site_id: uuid.UUID,
    session: SessionDep,
    disposition: Disposition | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> FlagPage:
    repository = SensitiveFlagRepository(session)
    try:
        items, total = repository.list_flags(
            org_id=org_id,
            site_id=site_id,
            content_id=content_id,
            disposition=disposition,
            limit=limit,
            offset=offset,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return FlagPage(
        items=[FlagOut.from_row(row) for row in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/sensitive-flags/{flag_id}", response_model=FlagOut, summary="One flagged fragment")
def get_flag(flag_id: uuid.UUID, org_id: uuid.UUID, site_id: uuid.UUID, session: SessionDep) -> FlagOut:
    try:
        row = SensitiveFlagRepository(session).get(flag_id, org_id=org_id, site_id=site_id)
    except FlagNotFound as error:
        raise HTTPException(status_code=404, detail="flag not found") from error
    return FlagOut.from_row(row)


@router.patch(
    "/sensitive-flags/{flag_id}",
    response_model=FlagOut,
    summary="Staff disposition: accept or dismiss one flag",
)
def review_flag(flag_id: uuid.UUID, payload: DispositionIn, session: SessionDep) -> FlagOut:
    repository = SensitiveFlagRepository(session)
    try:
        row = repository.set_disposition(
            flag_id,
            org_id=payload.org_id,
            site_id=payload.site_id,
            disposition=payload.disposition,
            reviewed_by=payload.reviewed_by,
            review_note=payload.review_note,
        )
    except FlagNotFound as error:
        raise HTTPException(status_code=404, detail="flag not found") from error
    except InvalidDisposition as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    session.commit()
    return FlagOut.from_row(row)


@router.delete(
    "/sensitive-flags/{flag_id}",
    status_code=204,
    summary="Soft-delete one flag (kept for audit, hidden from the queue)",
)
def delete_flag(
    flag_id: uuid.UUID, org_id: uuid.UUID, site_id: uuid.UUID, session: SessionDep
) -> Response:
    try:
        SensitiveFlagRepository(session).soft_delete(flag_id, org_id=org_id, site_id=site_id)
    except FlagNotFound as error:
        raise HTTPException(status_code=404, detail="flag not found") from error
    session.commit()
    return Response(status_code=204)


__all__ = ["router"]
