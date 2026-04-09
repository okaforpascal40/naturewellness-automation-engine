"""HMDB (Human Metabolome Database) API integration.

HMDB links metabolites/compounds to their dietary food sources, making it
particularly valuable for dietary bioactives such as berberine, quercetin,
resveratrol, and curcumin.

Two-step lookup:
  1. Search for compound by name to get HMDB accession ID
  2. Fetch metabolite details to extract food_sources field
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.models import FoodCompoundMapping

logger = logging.getLogger(__name__)

_BASE_URL = "https://hmdb.ca"
_SEARCH_URL = f"{_BASE_URL}/unearth/q"


def _extract_hmdb_id(result: dict[str, Any]) -> str | None:
    """Extract the HMDB accession ID from a search result record."""
    # The search API may return the accession under different keys
    for key in ("accession", "hmdb_id", "id"):
        value = result.get(key)
        if isinstance(value, str) and value.upper().startswith("HMDB"):
            return value
    return None


def _parse_food_sources(data: dict[str, Any], compound_name: str) -> list[FoodCompoundMapping]:
    """Parse the food_sources array from a metabolite detail response."""
    food_sources: Any = data.get("food_sources") or data.get("foodSources") or []

    if not isinstance(food_sources, list):
        return []

    mappings: list[FoodCompoundMapping] = []
    for entry in food_sources:
        if isinstance(entry, str):
            food_name = entry.strip()
            food_id = re.sub(r"\s+", "_", food_name.lower())
        elif isinstance(entry, dict):
            food_name = (
                entry.get("food_name")
                or entry.get("name")
                or entry.get("source_name")
                or ""
            ).strip()
            food_id = str(entry.get("id") or re.sub(r"\s+", "_", food_name.lower()))
        else:
            continue

        if not food_name:
            continue

        try:
            mappings.append(
                FoodCompoundMapping(
                    food_id=food_id,
                    food_name=food_name,
                    compound_name=compound_name,
                    compound_amount=None,
                    compound_unit=None,
                    source="hmdb",
                )
            )
        except Exception:
            logger.warning("Skipping malformed HMDB food_source entry: %s", entry)

    return mappings


async def search_foods_by_compound_hmdb(
    compound_name: str,
    limit: int = 10,
) -> list[FoodCompoundMapping]:
    """Return foods containing *compound_name* using HMDB.

    Two-step lookup:
    1. Search for the compound to get its HMDB accession ID.
    2. Fetch the metabolite detail record and parse the food_sources field.

    Returns an empty list if the compound is not found or has no food sources.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Step 1: Search for compound
        try:
            response = await client.get(
                _SEARCH_URL,
                params={"query": compound_name, "searcher": "metabolites"},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HMDB search failed for '%s' [%s]: %s",
                compound_name,
                exc.response.status_code,
                exc.response.text[:200],
            )
            return []
        except httpx.RequestError as exc:
            logger.warning("HMDB search request error for '%s': %s", compound_name, exc)
            return []

        try:
            search_data = response.json()
        except Exception:
            logger.warning("HMDB search returned non-JSON for '%s'", compound_name)
            return []

        # Normalise: the API may return a list or a dict wrapping a list
        results: list[Any] = []
        if isinstance(search_data, list):
            results = search_data
        elif isinstance(search_data, dict):
            for key in ("metabolites", "results", "data", "hits"):
                candidate = search_data.get(key)
                if isinstance(candidate, list):
                    results = candidate
                    break

        if not results:
            logger.debug("HMDB: no results for compound '%s'", compound_name)
            return []

        # Step 2: Get first result's HMDB accession ID
        hmdb_id = _extract_hmdb_id(results[0]) if isinstance(results[0], dict) else None
        if not hmdb_id:
            logger.debug("HMDB: could not extract accession from first result for '%s'", compound_name)
            return []

        # Step 3: Fetch metabolite details
        detail_url = f"{_BASE_URL}/metabolites/{hmdb_id}.json"
        try:
            detail_response = await client.get(detail_url)
            detail_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HMDB detail fetch failed for %s [%s]: %s",
                hmdb_id,
                exc.response.status_code,
                exc.response.text[:200],
            )
            return []
        except httpx.RequestError as exc:
            logger.warning("HMDB detail request error for %s: %s", hmdb_id, exc)
            return []

        try:
            detail_data = detail_response.json()
        except Exception:
            logger.warning("HMDB detail returned non-JSON for %s", hmdb_id)
            return []

        # Step 4: Parse food_sources
        mappings = _parse_food_sources(detail_data, compound_name)

        if mappings:
            logger.debug(
                "HMDB: %d food source(s) for '%s' (%s)", len(mappings), compound_name, hmdb_id
            )
        else:
            logger.debug("HMDB: no food sources in detail record for '%s' (%s)", compound_name, hmdb_id)

        return mappings[:limit]
