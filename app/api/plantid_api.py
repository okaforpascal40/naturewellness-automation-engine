"""Plant.id / Kindwise crop identification API integration (CamScan).

Docs: https://crop.kindwise.com/docs

A single POST to the identification endpoint returns a ranked list of candidate
species with confidence probabilities and (optionally) similar reference images.
We surface only the top suggestion, normalised to the shape the CamScan router
and frontend expect.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://crop.kindwise.com/api/v1/identification"

# Below this top-suggestion probability we still return the identification but
# flag it as low-confidence so the UI can warn the user.
_LOW_CONFIDENCE_THRESHOLD = 0.2

# Health-assessment tuning.
_MAX_CONDITIONS = 5
_MIN_CONDITION_PROBABILITY = 0.1   # below 10% the suggestion is noise
_HEALTH_DETAILS = ["description", "treatment", "url", "common_names"]

# Status codes that mean "the request shape was wrong", as opposed to auth or
# server faults. Only these trigger the identification-only retry.
_BAD_REQUEST_CODES = {400, 404, 422}


async def identify_plant(
    image_base64: str,
    api_key: str,
    api_url: str = _DEFAULT_URL,
) -> dict[str, Any]:
    """Identify a plant from a base64-encoded image via the Plant.id API.

    Returns a normalised dict:
        {
            "plant_name": str,
            "scientific_name": str,
            "confidence": float,        # 0–1
            "is_plant": bool,
            "plant_image_url": str | None,
            "common_names": list[str],
            "low_confidence": bool,     # only present/True when confidence < 0.2
        }

    Behaviour:
        - is_plant.binary is False  → {"is_plant": False}
        - confidence < 0.2          → result with "low_confidence": True
        - API / network errors      → raises RuntimeError with a clear message
    """
    if not api_key:
        raise RuntimeError("PLANTID_API_KEY is not configured.")
    if not image_base64:
        raise RuntimeError("No image provided for plant identification.")

    # The API accepts a bare base64 string or a data URI; strip any data-URI
    # prefix so we always send the raw payload.
    if image_base64.startswith("data:"):
        _, _, image_base64 = image_base64.partition(",")

    headers = {"Api-Key": api_key, "Content-Type": "application/json"}
    base_body: dict[str, Any] = {"images": [image_base64], "similar_images": True}
    # `health` is Plant.id v3 syntax. The configured endpoint defaults to
    # crop.kindwise.com, whose parameter set differs, so a rejection here must
    # not cost us the identification — see the retry below.
    health_body = {**base_body, "health": "all", "details": _HEALTH_DETAILS}

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(api_url, json=health_body, headers=headers)
            if response.status_code in _BAD_REQUEST_CODES:
                logger.info(
                    "Plant.id: health assessment rejected (%s); retrying identification only",
                    response.status_code,
                )
                response = await client.post(api_url, json=base_body, headers=headers)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] if exc.response is not None else ""
        logger.error(
            "Plant.id API error [%s]: %s",
            exc.response.status_code if exc.response is not None else "?",
            detail,
        )
        raise RuntimeError(
            f"Plant identification service returned an error "
            f"({exc.response.status_code if exc.response is not None else 'unknown'})."
        ) from exc
    except httpx.RequestError as exc:
        logger.error("Plant.id API request failed: %s", exc)
        raise RuntimeError(
            "Could not reach the plant identification service. Please try again."
        ) from exc
    except ValueError as exc:  # JSON decode
        logger.error("Plant.id API returned invalid JSON: %s", exc)
        raise RuntimeError("Plant identification service returned an invalid response.") from exc

    return _normalize_response(payload)


def _normalize_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a raw Plant.id response into the CamScan result shape."""
    result = payload.get("result") or {}

    # is_plant gate — the API reports a binary flag plus a probability.
    is_plant_info = result.get("is_plant") or {}
    if is_plant_info.get("binary") is False:
        logger.info("Plant.id: image is not a plant")
        return {"is_plant": False}

    classification = result.get("classification") or {}
    suggestions: list[dict[str, Any]] = classification.get("suggestions") or []
    if not suggestions:
        logger.info("Plant.id: no species suggestions returned")
        return {"is_plant": False}

    top = suggestions[0]
    confidence = float(top.get("probability", 0.0) or 0.0)

    scientific_name = (top.get("name") or "").strip()
    details = top.get("details") or {}
    common_names = [str(n).strip() for n in (details.get("common_names") or []) if str(n).strip()]

    # Prefer a human-friendly common name for the display label, fall back to
    # the scientific name.
    plant_name = common_names[0] if common_names else scientific_name

    plant_image_url = _first_similar_image_url(top.get("similar_images"))

    normalized: dict[str, Any] = {
        "plant_name": plant_name,
        "scientific_name": scientific_name,
        "confidence": round(confidence, 4),
        "is_plant": True,
        "plant_image_url": plant_image_url,
        "common_names": common_names,
    }

    if confidence < _LOW_CONFIDENCE_THRESHOLD:
        logger.info(
            "Plant.id: low-confidence identification (%.2f) for %s",
            confidence,
            scientific_name or "?",
        )
        normalized["low_confidence"] = True

    normalized.update(_normalize_health(result))
    return normalized


def _normalize_health(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the health assessment, if the response carries one.

    Written against two response shapes because the endpoint is configurable:
    Plant.id v3 nests disease suggestions under `result.disease`, while
    crop.kindwise uses `result.disease` or `result.crop` depending on product.
    Anything missing degrades to "no assessment available" rather than raising —
    identification is the feature that must not break.
    """
    conditions: list[dict[str, Any]] = []
    for key in ("disease", "health_assessment", "crop"):
        section = result.get(key)
        if not isinstance(section, dict):
            continue
        suggestions = section.get("suggestions") or section.get("diseases")
        if isinstance(suggestions, list) and suggestions:
            conditions = _parse_conditions(suggestions)
            if conditions:
                break

    is_healthy_info = result.get("is_healthy")
    if isinstance(is_healthy_info, dict):
        is_healthy = bool(is_healthy_info.get("binary", True))
        health_probability = _safe_float(is_healthy_info.get("probability"), 1.0)
    elif isinstance(is_healthy_info, bool):
        is_healthy = is_healthy_info
        health_probability = 1.0
    else:
        # No explicit flag: infer from whether anything harmful was suggested.
        is_healthy = not any(c["is_harmful"] for c in conditions)
        health_probability = 1.0 if is_healthy else 0.0

    return {
        "is_healthy": is_healthy,
        "health_probability": round(health_probability, 4),
        "plant_conditions": conditions,
        "health_assessment_available": bool(conditions) or isinstance(is_healthy_info, dict),
    }


def _parse_conditions(suggestions: list[Any]) -> list[dict[str, Any]]:
    """Map raw disease suggestions to the CamScan condition shape."""
    out: list[dict[str, Any]] = []
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        probability = _safe_float(suggestion.get("probability"), 0.0)
        if probability <= _MIN_CONDITION_PROBABILITY:
            continue

        details = suggestion.get("details") if isinstance(suggestion.get("details"), dict) else {}
        treatment_raw = details.get("treatment")
        treatment = treatment_raw if isinstance(treatment_raw, dict) else {}

        name = (suggestion.get("name") or "").strip()
        if not name:
            continue

        out.append(
            {
                "name": name,
                "probability": round(probability, 4),
                # Absent means unknown; assume harmful so the UI errs toward caution.
                "is_harmful": bool(suggestion.get("is_harmful", True)),
                "description": str(details.get("description") or "").strip(),
                "treatment": {
                    "biological": _join_treatment(treatment.get("biological")),
                    "chemical": _join_treatment(treatment.get("chemical")),
                    "prevention": _join_treatment(treatment.get("prevention")),
                },
                "url": str(details.get("url") or "").strip(),
            }
        )
        if len(out) >= _MAX_CONDITIONS:
            break
    return out


def _join_treatment(value: Any) -> str:
    """Treatment advice arrives as either a string or a list of steps."""
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_similar_image_url(similar_images: Any) -> str | None:
    """Return the best available reference image URL from a suggestion."""
    if not isinstance(similar_images, list):
        return None
    for image in similar_images:
        if not isinstance(image, dict):
            continue
        url = image.get("url_small") or image.get("url")
        if url:
            return str(url)
    return None
