"""ChEMBL API integration.

Docs: https://www.ebi.ac.uk/chembl/api/data/docs
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.models import CompoundGeneInteraction

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return get_settings().chembl_api_url.rstrip("/")


async def get_compounds_for_gene(
    gene_symbol: str,
    gene_id: str,
    limit: int = 20,
) -> list[CompoundGeneInteraction]:
    """Fetch bioactive compounds targeting a gene from ChEMBL."""
    # First resolve the gene symbol to a ChEMBL target
    target_url = f"{_base_url()}/target.json"
    target_params = {
        "target_synonym__icontains": gene_symbol,
        "target_type": "SINGLE PROTEIN",
        "organism": "Homo sapiens",
        "limit": 5,
        "format": "json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            target_resp = await client.get(target_url, params=target_params)
            target_resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ChEMBL target lookup failed for %s [%s]: %s",
                gene_symbol,
                exc.response.status_code,
                exc.response.text,
            )
            raise

        targets: list[Any] = (
            target_resp.json().get("targets", {}).get("target", [])
        )
        if not targets:
            logger.info("No ChEMBL targets found for gene %s", gene_symbol)
            return []

        target_chembl_id: str = targets[0].get("target_chembl_id", "")

        # Fetch activities for the resolved target
        activity_url = f"{_base_url()}/activity.json"
        activity_params = {
            "target_chembl_id": target_chembl_id,
            "limit": limit,
            "format": "json",
        }

        try:
            act_resp = await client.get(activity_url, params=activity_params)
            act_resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ChEMBL activity lookup failed [%s]: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise

    activities: list[Any] = act_resp.json().get("activities", {}).get("activity", [])
    interactions: list[CompoundGeneInteraction] = []

    for act in activities:
        compound_id = act.get("molecule_chembl_id")
        if not compound_id:
            continue
        try:
            interactions.append(
                CompoundGeneInteraction(
                    compound_id=compound_id,
                    compound_name=act.get("compound_name") or compound_id,
                    gene_id=gene_id,
                    gene_symbol=gene_symbol,
                    activity_type=act.get("standard_type"),
                    activity_value=_safe_float(act.get("standard_value")),
                    activity_units=act.get("standard_units"),
                    source="chembl",
                )
            )
        except Exception:
            logger.warning("Skipping malformed ChEMBL activity record: %s", act)

    return interactions


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
