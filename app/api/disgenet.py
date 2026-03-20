"""DisGeNET API integration.

Docs: https://disgenet.com/api
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.models import DiseaseGeneAssociation

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.disgenet.com/api/v1"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_settings().disgenet_api_key}",
        "Content-Type": "application/json",
    }


async def get_disease_gene_associations(
    disease_id: str,
    min_score: float = 0.3,
    limit: int = 10,
) -> list[DiseaseGeneAssociation]:
    """Fetch gene–disease associations from DisGeNET for a given disease UMLS ID."""
    url = f"{_BASE_URL}/gda/disease/{disease_id}"
    params: dict[str, Any] = {
        "min_score": min_score,
        "limit": limit,
        "format": "json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, headers=_headers(), params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "DisGeNET request failed [%s]: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise

    payload: dict[str, Any] = response.json()
    associations: list[DiseaseGeneAssociation] = []

    for item in payload.get("payload", []):
        try:
            associations.append(
                DiseaseGeneAssociation(
                    disease_id=disease_id,
                    disease_name=item.get("disease_name", ""),
                    gene_id=str(item.get("gene_ncbi_id", "")),
                    gene_symbol=item.get("gene_symbol", ""),
                    score=float(item.get("score", 0.0)),
                    source="disgenet",
                )
            )
        except Exception:
            logger.warning("Skipping malformed DisGeNET record: %s", item)

    return associations
