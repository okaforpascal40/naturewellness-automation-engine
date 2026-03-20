"""Reactome ContentService API integration.

Docs: https://reactome.org/ContentService/
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.models import GenePathwayMapping

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return get_settings().reactome_api_url.rstrip("/")


async def get_pathways_for_gene(
    gene_symbol: str,
    gene_id: str,
    species: str = "9606",  # NCBI taxonomy ID for Homo sapiens
) -> list[GenePathwayMapping]:
    """Fetch Reactome pathways associated with a gene symbol."""
    url = f"{_base_url()}/data/pathways/low/entity/{gene_symbol}/allForms"
    params = {"species": species}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Reactome request failed for gene %s [%s]: %s",
                gene_symbol,
                exc.response.status_code,
                exc.response.text,
            )
            raise

    items: list[Any] = response.json()
    mappings: list[GenePathwayMapping] = []

    for item in items:
        try:
            mappings.append(
                GenePathwayMapping(
                    gene_id=gene_id,
                    gene_symbol=gene_symbol,
                    pathway_id=item.get("stId", ""),
                    pathway_name=item.get("displayName", ""),
                    species="Homo sapiens",
                    source="reactome",
                )
            )
        except Exception:
            logger.warning("Skipping malformed Reactome record: %s", item)

    return mappings
