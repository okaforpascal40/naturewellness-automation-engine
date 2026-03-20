"""USDA FoodData Central API integration.

Docs: https://fdc.nal.usda.gov/api-guide.html
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.models import FoodCompoundMapping

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.nal.usda.gov/fdc/v1"


def _api_key() -> str:
    return get_settings().usda_api_key


async def search_foods_by_compound(
    compound_name: str,
    page_size: int = 10,
) -> list[FoodCompoundMapping]:
    """Search USDA FoodData Central for foods containing a given compound/nutrient."""
    url = f"{_BASE_URL}/foods/search"
    params = {
        "query": compound_name,
        "api_key": _api_key(),
        "pageSize": page_size,
        "dataType": "SR Legacy,Foundation,Survey (FNDDS)",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "USDA FDC request failed [%s]: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise

    foods: list[Any] = response.json().get("foods", [])
    mappings: list[FoodCompoundMapping] = []

    for food in foods:
        food_id = str(food.get("fdcId", ""))
        food_name = food.get("description", "")
        nutrients: list[Any] = food.get("foodNutrients", [])

        # Find the nutrient matching the compound
        for nutrient in nutrients:
            nutrient_name: str = nutrient.get("nutrientName", "")
            if compound_name.lower() in nutrient_name.lower():
                try:
                    mappings.append(
                        FoodCompoundMapping(
                            food_id=food_id,
                            food_name=food_name,
                            compound_name=nutrient_name,
                            compound_amount=_safe_float(nutrient.get("value")),
                            compound_unit=nutrient.get("unitName"),
                            source="usda",
                        )
                    )
                except Exception:
                    logger.warning(
                        "Skipping malformed USDA nutrient record: %s", nutrient
                    )
                break  # one match per food is sufficient

    return mappings


async def get_food_nutrients(food_id: str) -> dict[str, Any]:
    """Fetch full nutrient profile for a single FDC food item."""
    url = f"{_BASE_URL}/food/{food_id}"
    params = {"api_key": _api_key()}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "USDA FDC food detail request failed [%s]: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise

    return response.json()


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
