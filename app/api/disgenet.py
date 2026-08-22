"""Open Targets Platform API integration (replaces DisGeNET).

Docs: https://platform-docs.opentargets.org/data-access/graphql-api
GraphQL endpoint — no API key required.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.models import DiseaseGeneAssociation

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"

# Cap on supplementary genes so DisGeNET cannot dominate the ranked list.
_MAX_ADDITIONAL_GENES = 25

# Returns disease metadata + paginated target associations with overall scores.
_DISEASE_TARGETS_QUERY = """
query DiseaseTargets($diseaseId: String!, $page: Int!, $pageSize: Int!) {
  disease(efoId: $diseaseId) {
    id
    name
    associatedTargets(page: { index: $page, size: $pageSize }) {
      count
      rows {
        target {
          id
          approvedSymbol
          approvedName
        }
        score
      }
    }
  }
}
"""


async def _run_query(
    client: httpx.AsyncClient,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        _GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json"},
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Open Targets GraphQL request failed [%s]: %s",
            exc.response.status_code,
            exc.response.text,
        )
        raise
    payload: dict[str, Any] = response.json()
    if "errors" in payload:
        logger.error("Open Targets GraphQL errors: %s", payload["errors"])
        raise ValueError(f"GraphQL errors: {payload['errors']}")
    return payload.get("data", {})


async def get_additional_genes(disease_name: str) -> list[str]:
    """Extra disease-associated gene symbols from DisGeNET.

    Supplements Open Targets, which is scored and fairly conservative — DisGeNET
    curates from a wider literature base and surfaces genes Open Targets ranks
    below the cutoff.

    Requires `disgenet_api_key`. The free, unauthenticated endpoint this was
    originally specified against (www.disgenet.org/api) has been retired: it now
    serves the marketing site, and api.disgenet.com answers 401 without a key.
    With no key configured this returns [] and logs once, leaving the pipeline
    on Open Targets alone rather than failing the request.
    """
    name = (disease_name or "").strip()
    if not name:
        return []

    settings = get_settings()
    api_key = settings.disgenet_api_key
    if not api_key:
        logger.info(
            "DisGeNET skipped for %r — no disgenet_api_key configured "
            "(register at https://disgenet.com/ to enable)",
            name,
        )
        return []

    url = f"{settings.disgenet_api_url.rstrip('/')}/gda/summary"
    params = {
        "disease": name,
        "page_number": 0,
        # The API caps page size at 100.
        "page_size": min(_MAX_ADDITIONAL_GENES * 2, 100),
    }
    headers = {"Authorization": api_key, "Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "DisGeNET lookup failed for %r [%s]: %s",
            name,
            exc.response.status_code,
            exc.response.text[:200],
        )
        return []
    except Exception as exc:  # noqa: BLE001 - a supplement must never break the run
        logger.warning("DisGeNET lookup failed for %r: %s", name, exc)
        return []

    # Response envelope: {"status": ..., "payload": [{"symbolOfGene": ...}, ...]}
    rows = payload.get("payload")
    if not isinstance(rows, list):
        logger.warning("DisGeNET returned an unexpected payload for %r", name)
        return []

    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbolOfGene") or row.get("gene_symbol") or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
        if len(symbols) >= _MAX_ADDITIONAL_GENES:
            break

    logger.info("DisGeNET: %d additional gene(s) for %r", len(symbols), name)
    return symbols


async def get_disease_gene_associations(
    disease_id: str,
    min_score: float = 0.3,
    limit: int = 10,
) -> list[DiseaseGeneAssociation]:
    """Fetch gene–disease associations from Open Targets for a given EFO disease ID.

    Args:
        disease_id: EFO identifier (e.g. "EFO_0000400" for type 2 diabetes).
        min_score: Minimum overall association score in [0, 1].
        limit: Maximum number of associations to return.
    """
    associations: list[DiseaseGeneAssociation] = []
    page_size = min(limit, 50)  # Open Targets caps at 50 per page
    page = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        while len(associations) < limit:
            data = await _run_query(
                client,
                _DISEASE_TARGETS_QUERY,
                {"diseaseId": disease_id, "page": page, "pageSize": page_size},
            )

            disease_data: dict[str, Any] | None = data.get("disease")
            if not disease_data:
                logger.warning(
                    "Open Targets returned no disease data for id '%s'", disease_id
                )
                break

            disease_name: str = disease_data.get("name", disease_id)
            associated = disease_data.get("associatedTargets", {})
            rows: list[dict[str, Any]] = associated.get("rows", [])
            total: int = associated.get("count", 0)

            if not rows:
                break

            for row in rows:
                if len(associations) >= limit:
                    break

                score = float(row.get("score", 0.0))
                if score < min_score:
                    continue

                target = row.get("target", {})
                try:
                    associations.append(
                        DiseaseGeneAssociation(
                            disease_id=disease_id,
                            disease_name=disease_name,
                            gene_id=target.get("id", ""),
                            gene_symbol=target.get("approvedSymbol", ""),
                            score=score,
                            source="open_targets",
                        )
                    )
                except Exception:
                    logger.warning(
                        "Skipping malformed Open Targets association row: %s", row
                    )

            # Stop if we have fetched all available results
            fetched_so_far = (page + 1) * page_size
            if fetched_so_far >= total or len(rows) < page_size:
                break

            page += 1

    return associations
