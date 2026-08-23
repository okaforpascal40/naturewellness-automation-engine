"""Recovering disease-gene associations for conditions Open Targets scores poorly.

Two distinct failure modes, handled separately because they have different fixes:

1. The disease HAS associations but every one scores below our cutoff. Typhoid
   fever is the case that motivated this: Open Targets lists 284 targets for it
   and the best scores 0.05, against a default `min_score` of 0.3. Lowering the
   bar for that disease recovers real data with real citations.

2. The disease entity we were handed is the wrong one, or a thinly-curated
   sibling of a better-covered entity. Searching a synonym can land on an entity
   with usable associations.

Both return genuine Open Targets rows. Neither invents anything.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.models import DiseaseGeneAssociation

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"

# Alternative phrasings to search when the supplied disease yields nothing.
# Keys are matched as substrings of the lowercased disease name.
DISEASE_SYNONYMS: dict[str, list[str]] = {
    "typhoid": ["salmonella infection", "enteric fever", "salmonella typhi"],
    "malaria": ["plasmodium infection", "malaria falciparum"],
    "cholera": ["vibrio cholerae infection", "cholera infection"],
    "tuberculosis": [
        "mycobacterium tuberculosis infection",
        "pulmonary tuberculosis",
        "TB infection",
    ],
    "hiv": ["HIV infection", "AIDS", "human immunodeficiency virus infection"],
    "hepatitis": ["hepatitis B virus infection", "hepatitis C virus infection"],
    "pneumonia": ["bacterial pneumonia", "streptococcal pneumonia"],
    "dengue": ["dengue fever", "dengue virus infection"],
    "covid": ["COVID-19", "SARS-CoV-2 infection", "coronavirus disease"],
    "influenza": ["influenza infection", "influenza A", "flu"],
    "meningitis": ["bacterial meningitis", "meningococcal disease"],
    "sepsis": ["septicemia", "bloodstream infection", "bacteremia"],
}

# Progressively weaker score floors. 0.05 is the lowest we will accept: below
# that Open Targets associations are dominated by single weak text-mining hits.
_FALLBACK_SCORE_FLOORS = (0.1, 0.05)

_SEARCH_QUERY = """
query SearchDisease($q: String!) {
  search(queryString: $q, entityNames: ["disease"], page: { index: 0, size: 3 }) {
    hits { id name }
  }
}
"""


def synonyms_for(disease_name: str) -> list[str]:
    """Alternative names to try for a disease, or [] when none are registered."""
    lowered = (disease_name or "").strip().lower()
    if not lowered:
        return []
    for key, values in DISEASE_SYNONYMS.items():
        if key in lowered:
            return list(values)
    return []


async def retry_with_lower_threshold(
    disease_id: str,
    original_min_score: float,
    limit: int,
) -> tuple[list[DiseaseGeneAssociation], float | None]:
    """Re-query the same disease at progressively lower score floors.

    Returns (associations, floor_used). Tried before synonyms because it keeps
    the disease the caller actually asked about.
    """
    from app.api import disgenet  # local import avoids a circular dependency

    for floor in _FALLBACK_SCORE_FLOORS:
        if floor >= original_min_score:
            continue
        try:
            found = await disgenet.get_disease_gene_associations(
                disease_id=disease_id, min_score=floor, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - a fallback must not raise
            logger.warning("Threshold retry failed for %s at %.2f: %s", disease_id, floor, exc)
            continue
        if found:
            logger.info(
                "Recovered %d gene(s) for %s by lowering the score floor to %.2f",
                len(found),
                disease_id,
                floor,
            )
            return found, floor
    return [], None


async def search_disease_id(query: str) -> tuple[str, str] | None:
    """Resolve a free-text disease name to the top Open Targets (id, name)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                _GRAPHQL_URL,
                json={"query": _SEARCH_QUERY, "variables": {"q": query}},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
    except Exception as exc:  # noqa: BLE001 - a fallback must not raise
        logger.warning("Open Targets search failed for %r: %s", query, exc)
        return None

    hits = (((payload.get("data") or {}).get("search") or {}).get("hits")) or []
    for hit in hits:
        hit_id = (hit.get("id") or "").strip()
        hit_name = (hit.get("name") or "").strip()
        if hit_id:
            return hit_id, hit_name
    return None


async def try_disease_synonyms(
    disease_name: str,
    min_score: float,
    limit: int,
    exclude_id: str = "",
) -> tuple[list[DiseaseGeneAssociation], str]:
    """Search each registered synonym until one yields associations.

    Returns (associations, label_of_the_synonym_used). The label is surfaced to
    the reader — they asked about one condition and got answers about a
    related one, which they need to be told.
    """
    from app.api import disgenet  # local import avoids a circular dependency

    for synonym in synonyms_for(disease_name):
        found_id = await search_disease_id(synonym)
        if not found_id:
            continue
        disease_id, resolved_name = found_id
        if disease_id == exclude_id:
            continue

        try:
            associations = await disgenet.get_disease_gene_associations(
                disease_id=disease_id, min_score=min_score, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - a fallback must not raise
            logger.warning("Synonym lookup failed for %r: %s", synonym, exc)
            continue

        if not associations:
            associations, _floor = await retry_with_lower_threshold(
                disease_id, min_score, limit
            )

        if associations:
            label = resolved_name or synonym
            logger.info(
                "Recovered %d gene(s) for %r via synonym %r (%s)",
                len(associations),
                disease_name,
                synonym,
                disease_id,
            )
            return associations, label

    return [], ""
