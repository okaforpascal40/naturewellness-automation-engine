"""Manual curation / review endpoints."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from app.database import fetch_records, insert_record, update_record
from app.models import (
    ManualReviewStatus,
    PaginatedResponse,
    ReviewRecord,
    ReviewUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/review", tags=["manual_review"])


@router.post(
    "/",
    response_model=ReviewRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Create a review record for an evidence score",
)
async def create_review(evidence_score_id: str) -> ReviewRecord:
    """Initialise a pending review entry for a given evidence score."""
    payload = {
        "evidence_score_id": evidence_score_id,
        "status": ManualReviewStatus.pending.value,
    }
    try:
        row = await insert_record("review_records", payload)
    except Exception as exc:
        logger.exception("Failed to create review record")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error: {exc}",
        ) from exc
    return ReviewRecord(**row)


@router.get(
    "/",
    response_model=PaginatedResponse,
    summary="List review records with optional status filter",
)
async def list_reviews(
    review_status: ManualReviewStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PaginatedResponse:
    filters = {"status": review_status.value} if review_status else None
    offset = (page - 1) * page_size

    try:
        rows = await fetch_records(
            "review_records", filters=filters, limit=page_size, offset=offset
        )
        # Count is approximate; a dedicated count query could be added
        total = len(rows) + offset
    except Exception as exc:
        logger.exception("Failed to fetch review records")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error: {exc}",
        ) from exc

    items = [ReviewRecord(**row) for row in rows]
    pages = max(1, -(-total // page_size))  # ceiling division
    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size, pages=pages)


@router.get(
    "/{review_id}",
    response_model=ReviewRecord,
    summary="Get a single review record",
)
async def get_review(review_id: str) -> ReviewRecord:
    try:
        rows = await fetch_records("review_records", filters={"id": review_id}, limit=1)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error: {exc}",
        ) from exc

    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    return ReviewRecord(**rows[0])


@router.patch(
    "/{review_id}",
    response_model=ReviewRecord,
    summary="Update the status / notes on a review record",
)
async def update_review(review_id: str, body: ReviewUpdateRequest) -> ReviewRecord:
    update_payload: dict = {
        "status": body.status.value,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    if body.reviewer_notes is not None:
        update_payload["reviewer_notes"] = body.reviewer_notes
    if body.reviewed_by is not None:
        update_payload["reviewed_by"] = body.reviewed_by

    try:
        row = await update_record("review_records", review_id, update_payload)
    except Exception as exc:
        logger.exception("Failed to update review %s", review_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error: {exc}",
        ) from exc
    return ReviewRecord(**row)


@router.get(
    "/pending/count",
    response_model=dict,
    summary="Count pending reviews",
)
async def pending_count() -> dict:
    try:
        rows = await fetch_records(
            "review_records",
            filters={"status": ManualReviewStatus.pending.value},
            limit=1000,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error: {exc}",
        ) from exc
    return {"pending": len(rows)}
