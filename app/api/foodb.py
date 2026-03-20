"""FooDB API integration.

Docs: https://foodb.ca
FooDB covers bioactive phytochemicals (polyphenols, flavonoids, etc.)
that USDA FoodData Central often lacks.  Two-step lookup:
  1. Resolve compound name → FooDB compound ID
  2. Fetch foods that contain the compound
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.models import FoodCompoundMapping

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return get_settings().foodb_api_url.rstrip("/")


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> Any:
    """Shared GET helper with raise_for_status."""
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


async def _resolve_compound_id(client: httpx.AsyncClient, compound_name: str) -> int | None:
    """Search FooDB for a compound by name and return its integer ID."""
    url = f"{_base_url()}/compounds/search.json"
    try:
        data = await _get(client, url, {"query": compound_name})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        logger.error(
            "FooDB compound search failed for '%s' [%s]: %s",
            compound_name,
            exc.response.status_code,
            exc.response.text,
        )
        raise

    if not isinstance(data, list) or not data:
        return None

    # Prefer an exact (case-insensitive) name match; fall back to first result.
    for item in data:
        if isinstance(item, dict) and item.get("name", "").lower() == compound_name.lower():
            return item.get("id")
    return data[0].get("id") if isinstance(data[0], dict) else None


async def _get_foods_for_compound(
    client: httpx.AsyncClient,
    compound_id: int,
    compound_name: str,
    limit: int,
) -> list[FoodCompoundMapping]:
    """Fetch foods that contain a given FooDB compound ID."""
    url = f"{_base_url()}/foods.json"
    try:
        data = await _get(client, url, {"compound_id": compound_id})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        logger.error(
            "FooDB food lookup failed for compound_id=%s [%s]: %s",
            compound_id,
            exc.response.status_code,
            exc.response.text,
        )
        raise

    if not isinstance(data, list):
        return []

    mappings: list[FoodCompoundMapping] = []
    for item in data[:limit]:
        if not isinstance(item, dict):
            continue
        food_id = item.get("id")
        food_name = item.get("name") or item.get("name_scientific")
        if not food_id or not food_name:
            continue
        try:
            mappings.append(
                FoodCompoundMapping(
                    food_id=str(food_id),
                    food_name=food_name,
                    compound_name=compound_name,
                    compound_amount=_safe_float(item.get("concentration_avg")),
                    compound_unit=item.get("concentration_unit"),
                    source="foodb",
                )
            )
        except Exception:
            logger.warning("Skipping malformed FooDB food record: %s", item)

    return mappings


async def search_foods_by_compound(
    compound_name: str,
    limit: int = 10,
) -> list[FoodCompoundMapping]:
    """Return foods containing *compound_name* using the FooDB database.

    Performs two sequential HTTP requests with a 0.5 s pause between them
    to respect FooDB's rate limits.  Returns an empty list on 404 or if
    the compound is not found in FooDB.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        compound_id = await _resolve_compound_id(client, compound_name)
        if compound_id is None:
            logger.debug("FooDB: compound '%s' not found", compound_name)
            return []

        # Brief pause between requests to respect rate limits.
        await asyncio.sleep(0.5)

        return await _get_foods_for_compound(client, compound_id, compound_name, limit)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
