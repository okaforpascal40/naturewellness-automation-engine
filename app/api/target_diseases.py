"""Open Targets reverse lookup: gene symbols → associated diseases.

The forward pipeline (app/api/disgenet.py) asks Open Targets for a disease's
targets. Plant search needs the opposite direction, which takes two steps:
  1. mapIds              — batch-resolve HGNC symbols to Ensembl target ids
  2. associatedDiseases  — per target, its top-scored diseases (concurrent)

Everything here is best-effort. A failed mapping or target query is logged and
skipped; the caller gets whatever succeeded, possibly nothing, never an error.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 10.0
# Overall deadline for mapIds + every target query; per-request timeouts alone
# let 4 sequential waves of slow calls run for 40s+.
_TOTAL_BUDGET_SECONDS = 15.0
_MAX_CONCURRENCY = 5
_DEFAULT_MIN_SCORE = 0.3
_DEFAULT_DISEASES_PER_GENE = 15
# Open Targets files quantitative traits ("body mass index", "HbA1c
# measurement") under this therapeutic area; they are not conditions.
_MEASUREMENT_AREA = "EFO_0001444"
# Human Phenotype Ontology terms ("Abnormal urine sodium concentration") are
# findings, not conditions, and have no results page of their own.
_PHENOTYPE_PREFIX = "HP_"

_MAP_IDS_QUERY = """
query MapTargets($terms: [String!]!) {
  mapIds(queryTerms: $terms, entityNames: ["target"]) {
    mappings { term hits { id name entity } }
  }
}
"""

_TARGET_DISEASES_QUERY = """
query TargetDiseases($ensemblId: String!, $size: Int!) {
  target(ensemblId: $ensemblId) {
    approvedSymbol
    associatedDiseases(page: { index: 0, size: $size }) {
      rows {
        score
        disease { id name therapeuticAreas { id } }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class GeneDisease:
    gene_symbol: str
    disease_id: str
    disease_name: str
    score: float


def make_client() -> httpx.AsyncClient:
    """HTTP client for Open Targets — a seam tests replace with a mock transport."""
    return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)


async def _run_query(
    client: httpx.AsyncClient, query: str, variables: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        get_settings().open_targets_api_url,
        json={"query": query, "variables": variables},
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    if payload.get("errors"):
        raise ValueError(f"GraphQL errors: {payload['errors']}")
    return payload.get("data") or {}


async def _map_symbols(client: httpx.AsyncClient, symbols: list[str]) -> dict[str, str]:
    """{symbol: ensembl_id} for symbols with an exact approved-symbol hit."""
    data = await _run_query(client, _MAP_IDS_QUERY, {"terms": symbols})
    mapped: dict[str, str] = {}
    for mapping in (data.get("mapIds") or {}).get("mappings") or []:
        term = str(mapping.get("term") or "").upper()
        for hit in mapping.get("hits") or []:
            # mapIds can return near-misses; only an exact symbol is trusted.
            if hit.get("entity") == "target" and str(hit.get("name") or "").upper() == term:
                mapped[term] = str(hit.get("id"))
                break
    return mapped


async def _diseases_for_target(
    client: httpx.AsyncClient,
    symbol: str,
    ensembl_id: str,
    size: int,
    min_score: float,
) -> list[GeneDisease]:
    data = await _run_query(
        client, _TARGET_DISEASES_QUERY, {"ensemblId": ensembl_id, "size": size}
    )
    rows = ((data.get("target") or {}).get("associatedDiseases") or {}).get("rows") or []
    results: list[GeneDisease] = []
    for row in rows:
        disease = row.get("disease") or {}
        disease_id = str(disease.get("id") or "").strip()
        name = str(disease.get("name") or "").strip()
        score = float(row.get("score") or 0.0)
        areas = {str(a.get("id")) for a in disease.get("therapeuticAreas") or []}
        if (
            not disease_id
            or not name
            or score < min_score
            or _MEASUREMENT_AREA in areas
            or disease_id.startswith(_PHENOTYPE_PREFIX)
        ):
            continue
        results.append(GeneDisease(symbol, disease_id, name, score))
    return results


async def get_diseases_for_genes(
    gene_symbols: list[str],
    *,
    diseases_per_gene: int = _DEFAULT_DISEASES_PER_GENE,
    min_score: float = _DEFAULT_MIN_SCORE,
    budget_seconds: float | None = None,
) -> list[GeneDisease]:
    """Diseases Open Targets associates with each gene, flattened.

    Bounded: one mapIds call, then at most ``_MAX_CONCURRENCY`` target queries
    in flight, each with its own timeout, all within ``budget_seconds`` overall
    (default ``_TOTAL_BUDGET_SECONDS``). Returns whatever finished in time, or
    [] if Open Targets is down.
    """
    if budget_seconds is None:
        budget_seconds = _TOTAL_BUDGET_SECONDS
    symbols = list(dict.fromkeys(s.strip().upper() for s in gene_symbols if s and s.strip()))
    if not symbols:
        return []

    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds

    async with make_client() as client:
        try:
            ensembl_ids = await asyncio.wait_for(_map_symbols(client, symbols), budget_seconds)
        except Exception as exc:  # noqa: BLE001 - degrade to no conditions (incl. timeout)
            logger.warning("Open Targets symbol mapping failed: %r", exc)
            return []

        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def one(symbol: str, ensembl_id: str) -> list[GeneDisease]:
            async with semaphore:
                try:
                    return await _diseases_for_target(
                        client, symbol, ensembl_id, diseases_per_gene, min_score
                    )
                except Exception as exc:  # noqa: BLE001 - skip this gene only
                    logger.warning("Open Targets diseases lookup failed for %s: %s", symbol, exc)
                    return []

        tasks = [
            asyncio.create_task(one(sym, ensembl_ids[sym])) for sym in symbols if sym in ensembl_ids
        ]
        remaining = deadline - loop.time()
        done: set[asyncio.Task[list[GeneDisease]]] = set()
        if tasks and remaining > 0:
            done, pending = await asyncio.wait(tasks, timeout=remaining)
        else:
            pending = set(tasks)
        if pending:
            # The per-request timeout bounds each call, not the whole fan-out;
            # past the budget, keep what finished rather than stall the search.
            logger.warning(
                "Open Targets: %d of %d gene lookups exceeded the %.0fs budget; skipped",
                len(pending), len(tasks), budget_seconds,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        batches = [task.result() for task in tasks if task in done]

    unmapped = [s for s in symbols if s not in ensembl_ids]
    if unmapped:
        logger.info("Open Targets: no target id for %s", ", ".join(unmapped))
    return [item for batch in batches for item in batch]
