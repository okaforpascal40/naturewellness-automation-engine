"""Automation endpoints — trigger and retrieve pipeline runs."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.core.chain_builder import run_pipeline
from app.database import fetch_records, insert_record
from app.models import AutomationRunRequest, AutomationRunResponse, EvidenceScore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automation", tags=["automation"])


@router.post(
    "/run",
    response_model=AutomationRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Run the full Disease→Gene→Pathway→Compound→Food pipeline",
)
async def run_automation(request: AutomationRunRequest) -> AutomationRunResponse:
    """Execute the automated evidence-scoring pipeline for a given disease."""
    try:
        result = await run_pipeline(request)
    except Exception as exc:
        logger.exception("Pipeline failed for disease %s", request.disease_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Pipeline execution error: {exc}",
        ) from exc

    # Persist scores to Supabase (best-effort; don't fail the response)
    for score in result.evidence_scores:
        try:
            await insert_record("evidence_scores", score.model_dump(exclude={"id"}))
        except Exception as exc:
            logger.warning("Failed to persist evidence score: %s", exc)

    return result


@router.get(
    "/scores",
    response_model=list[EvidenceScore],
    summary="List stored evidence scores",
)
async def list_evidence_scores(
    disease_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[EvidenceScore]:
    """Retrieve persisted evidence scores, optionally filtered by disease."""
    filters = {"disease_id": disease_id} if disease_id else None
    try:
        rows = await fetch_records("evidence_scores", filters=filters, limit=limit, offset=offset)
    except Exception as exc:
        logger.exception("Failed to fetch evidence scores")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error: {exc}",
        ) from exc
    return [EvidenceScore(**row) for row in rows]


@router.get(
    "/scores/{score_id}",
    response_model=EvidenceScore,
    summary="Get a single evidence score by ID",
)
async def get_evidence_score(score_id: str) -> EvidenceScore:
    try:
        rows = await fetch_records("evidence_scores", filters={"id": score_id}, limit=1)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error: {exc}",
        ) from exc

    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Score not found")
    return EvidenceScore(**rows[0])
