"""ChEMBL API integration — natural products only.

Docs: https://www.ebi.ac.uk/chembl/api/data/docs

The pipeline only wants food bioactives (quercetin, resveratrol, curcumin…),
not synthetic drugs or clinical candidates.  ChEMBL's natural_product flag
lives on the molecule record, so filtering requires two requests:
  1. Fetch activities for the target  → collect candidate molecule IDs
  2. Batch-query the molecule endpoint → keep only natural products
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.models import CompoundGeneInteraction

logger = logging.getLogger(__name__)

# Compounds in late-stage clinical trials or already approved are almost
# certainly pharmaceuticals, not dietary compounds.  Skip max_phase >= this.
_MAX_PHASE_CUTOFF = 2


def _base_url() -> str:
    return get_settings().chembl_api_url.rstrip("/")


async def get_compounds_for_gene(
    gene_symbol: str,
    gene_id: str,
    limit: int = 20,
) -> list[CompoundGeneInteraction]:
    """Fetch natural-product bioactive compounds targeting a gene from ChEMBL.

    Two-step process:
      1. Resolve gene symbol → ChEMBL target ID
      2. Fetch activities, then filter to natural products via molecule endpoint
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        target_chembl_id = await _resolve_target(client, gene_symbol)
        if not target_chembl_id:
            return []

        candidate_activities = await _fetch_activities(client, target_chembl_id, fetch_limit=limit * 3)
        if not candidate_activities:
            return []

        natural_ids = await _filter_natural_products(
            client,
            molecule_ids=[a["molecule_chembl_id"] for a in candidate_activities],
        )

    interactions: list[CompoundGeneInteraction] = []
    for act in candidate_activities:
        if len(interactions) >= limit:
            break

        compound_id: str = act["molecule_chembl_id"]
        if compound_id not in natural_ids:
            logger.debug("Skipping non-natural compound %s", compound_id)
            continue

        compound_name: str = natural_ids[compound_id]
        try:
            interactions.append(
                CompoundGeneInteraction(
                    compound_id=compound_id,
                    compound_name=compound_name,
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

    logger.info(
        "ChEMBL: %d natural-product interaction(s) found for gene %s (from %d candidates)",
        len(interactions),
        gene_symbol,
        len(candidate_activities),
    )
    return interactions


async def _resolve_target(client: httpx.AsyncClient, gene_symbol: str) -> str | None:
    """Return the first matching ChEMBL target ID for a gene symbol."""
    url = f"{_base_url()}/target.json"
    params = {
        "target_synonym__icontains": gene_symbol,
        "target_type": "SINGLE PROTEIN",
        "organism": "Homo sapiens",
        "limit": 5,
        "format": "json",
    }
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "ChEMBL target lookup failed for %s [%s]: %s",
            gene_symbol,
            exc.response.status_code,
            exc.response.text,
        )
        raise

    # ChEMBL envelope: {"targets": [...], "page_meta": {...}}
    targets: list[Any] = resp.json().get("targets", [])
    if not targets:
        logger.info("No ChEMBL targets found for gene %s", gene_symbol)
        return None
    return targets[0].get("target_chembl_id", "")


async def _fetch_activities(
    client: httpx.AsyncClient,
    target_chembl_id: str,
    fetch_limit: int,
) -> list[dict[str, Any]]:
    """Fetch activity records for a target, returning only rows with a molecule name."""
    url = f"{_base_url()}/activity.json"
    params = {
        "target_chembl_id": target_chembl_id,
        "limit": fetch_limit,
        "format": "json",
    }
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "ChEMBL activity lookup failed for target %s [%s]: %s",
            target_chembl_id,
            exc.response.status_code,
            exc.response.text,
        )
        raise

    # ChEMBL envelope: {"activities": [...], "page_meta": {...}}
    activities: list[Any] = resp.json().get("activities", [])

    # Deduplicate by molecule ID and require a preferred name.
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for act in activities:
        mid = act.get("molecule_chembl_id")
        if not mid or mid in seen:
            continue
        if not act.get("molecule_pref_name"):
            logger.debug("Skipping %s — no preferred name", mid)
            continue
        seen.add(mid)
        result.append(act)

    return result


async def _filter_natural_products(
    client: httpx.AsyncClient,
    molecule_ids: list[str],
) -> dict[str, str]:
    """Fetch molecule records and return {chembl_id: pref_name} for natural products.

    Filtering is done client-side after fetching, because the ChEMBL molecule
    endpoint rejects molecule_properties__natural_product and max_phase__lt as
    query parameters with a 400 error.

    Rules (each field is checked only when present in the response):
      - molecule_properties.natural_product == 1  → keep
      - molecule_properties.natural_product == 0  → skip (confirmed synthetic)
      - field absent                              → accept (data not available)
      - max_phase >= _MAX_PHASE_CUTOFF            → skip (late-stage drug)
      - max_phase < _MAX_PHASE_CUTOFF or None     → accept
    """
    if not molecule_ids:
        return {}

    url = f"{_base_url()}/molecule.json"
    params = {
        "molecule_chembl_id__in": ",".join(molecule_ids),
        "limit": len(molecule_ids),
        "format": "json",
    }
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "ChEMBL molecule fetch failed [%s]: %s",
            exc.response.status_code,
            exc.response.text,
        )
        raise

    # ChEMBL envelope: {"molecules": [...], "page_meta": {...}}
    molecules: list[Any] = resp.json().get("molecules", [])

    result: dict[str, str] = {}
    for m in molecules:
        if not isinstance(m, dict):
            continue
        chembl_id: str | None = m.get("molecule_chembl_id")
        if not chembl_id:
            continue

        # --- natural_product flag ---
        props: dict[str, Any] = m.get("molecule_properties") or {}
        np_flag = props.get("natural_product")
        if np_flag is not None and int(np_flag) != 1:
            logger.debug("Skipping %s — confirmed synthetic (natural_product=%s)", chembl_id, np_flag)
            continue

        # --- max_phase filter ---
        max_phase = m.get("max_phase")
        if max_phase is not None:
            try:
                if float(max_phase) >= _MAX_PHASE_CUTOFF:
                    logger.debug("Skipping %s — max_phase=%s", chembl_id, max_phase)
                    continue
            except (TypeError, ValueError):
                pass  # unparseable max_phase → accept

        result[chembl_id] = m.get("pref_name") or chembl_id

    logger.debug(
        "Natural product filter: %d/%d molecules passed", len(result), len(molecules)
    )
    return result


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
