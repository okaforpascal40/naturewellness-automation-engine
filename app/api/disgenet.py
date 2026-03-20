"""Open Targets Platform API integration (replaces DisGeNET).

Docs: https://platform-docs.opentargets.org/data-access/graphql-api
GraphQL endpoint — no API key required.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.models import DiseaseGeneAssociation

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"

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
