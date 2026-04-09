"""Step 1 — Fetch top disease-associated genes from Open Targets.

Queries Open Targets GraphQL API for the top-scoring genes across 14 disease
categories, enriches each gene record with a description via MyGene.info, then
bulk-inserts into the Supabase `genes` table.

Usage:
    python data_population/fetch_top_disease_genes.py [--dry-run]

Target: 200+ unique gene rows in the genes table.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import httpx

from utils import (
    RateLimiter,
    batch_insert,
    get_existing_values,
    load_checkpoint,
    safe_get,
    save_checkpoint,
    setup_logging,
)

# ── Config ────────────────────────────────────────────────────────────────────

OPEN_TARGETS_URL = "https://api.platform.opentargets.org/api/v4/graphql"
MYGENE_URL       = "https://mygene.info/v3"
CHECKPOINT_NAME  = "fetch_genes"
GENES_PER_DISEASE = 20          # fetch this many per disease; dedup gives ~200 unique

DISEASE_CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "diabetes": [
        ("MONDO_0005148", "Type 2 Diabetes Mellitus"),
        ("MONDO_0005147", "Type 1 Diabetes Mellitus"),
    ],
    "cardiovascular": [
        ("MONDO_0005010", "Coronary Artery Disease"),
        ("MONDO_0005044", "Hypertension"),
        ("EFO_0003144",   "Heart Failure"),
    ],
    "cancer": [
        ("MONDO_0007254", "Breast Cancer"),
        ("EFO_0001071",   "Lung Cancer"),
        ("MONDO_0005575", "Colorectal Cancer"),
        ("EFO_0001663",   "Prostate Cancer"),
    ],
    "neurological": [
        ("MONDO_0004975", "Alzheimer's Disease"),
        ("MONDO_0005180", "Parkinson's Disease"),
    ],
    "inflammatory": [
        ("EFO_0000685", "Rheumatoid Arthritis"),
        ("EFO_0003767", "Inflammatory Bowel Disease"),
    ],
    "autoimmune": [
        ("EFO_0003885", "Multiple Sclerosis"),
        ("EFO_0002690", "Systemic Lupus Erythematosus"),
    ],
}

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
          functionDescriptions
        }
        score
      }
    }
  }
}
"""

logger = setup_logging("fetch_genes")


# ── Open Targets helpers ──────────────────────────────────────────────────────

async def _run_ot_query(
    client: httpx.AsyncClient,
    disease_id: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    resp = await client.post(
        OPEN_TARGETS_URL,
        json={
            "query": _DISEASE_TARGETS_QUERY,
            "variables": {"diseaseId": disease_id, "page": page, "pageSize": page_size},
        },
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    payload: dict[str, Any] = resp.json()
    if "errors" in payload:
        raise ValueError(f"Open Targets GraphQL error: {payload['errors']}")
    return payload.get("data", {})


async def fetch_genes_for_disease(
    client: httpx.AsyncClient,
    disease_id: str,
    disease_name: str,
    limit: int,
    min_score: float = 0.1,
) -> list[dict[str, Any]]:
    """Return up to *limit* gene records for a disease from Open Targets."""
    results: list[dict[str, Any]] = []
    page_size = min(limit, 50)
    page = 0

    while len(results) < limit:
        try:
            data = await _run_ot_query(client, disease_id, page, page_size)
        except Exception as exc:
            logger.warning("OT query failed for %s: %s", disease_id, exc)
            break

        disease_data = data.get("disease")
        if not disease_data:
            logger.warning("No disease data for %s", disease_id)
            break

        associated = disease_data.get("associatedTargets", {})
        rows: list[dict[str, Any]] = associated.get("rows", [])
        total: int = associated.get("count", 0)

        for row in rows:
            if len(results) >= limit:
                break
            score = float(row.get("score", 0.0))
            if score < min_score:
                continue
            target = row.get("target", {})
            descriptions: list[str] = target.get("functionDescriptions") or []
            results.append({
                "ensembl_id":  target.get("id", ""),
                "gene_symbol": target.get("approvedSymbol", ""),
                "gene_name":   target.get("approvedName", ""),
                "description": descriptions[0] if descriptions else "",
                "_ot_score":   score,          # internal, not stored
                "_disease":    disease_name,   # internal, not stored
            })

        fetched_so_far = (page + 1) * page_size
        if fetched_so_far >= total or len(rows) < page_size:
            break
        page += 1

    logger.info(
        "Open Targets: %d gene(s) for %s (%s)", len(results), disease_name, disease_id
    )
    return results


# ── MyGene.info enrichment ────────────────────────────────────────────────────

async def enrich_with_mygene(
    client: httpx.AsyncClient,
    ensembl_ids: list[str],
    rate: RateLimiter,
    batch_size: int = 50,
) -> dict[str, dict[str, Any]]:
    """Return {ensembl_id: {gene_name, description}} for a list of Ensembl IDs.

    Uses the MyGene.info bulk query endpoint — one POST per batch.
    Falls back gracefully if the service is unavailable.
    """
    results: dict[str, dict[str, Any]] = {}
    url = f"{MYGENE_URL}/query"

    for i in range(0, len(ensembl_ids), batch_size):
        chunk = ensembl_ids[i : i + batch_size]
        await rate.wait()
        try:
            resp = await client.post(
                url,
                data={
                    "q":       ",".join(chunk),
                    "scopes":  "ensembl.gene",
                    "fields":  "name,summary,entrezgene",
                    "species": "human",
                },
            )
            resp.raise_for_status()
            hits: list[dict[str, Any]] = resp.json()
            for hit in hits:
                query_id: str = hit.get("query", "")
                if hit.get("notfound"):
                    continue
                results[query_id] = {
                    "gene_name":   hit.get("name", ""),
                    "description": hit.get("summary", ""),
                }
        except Exception as exc:
            logger.warning("MyGene.info batch failed (offset %d): %s", i, exc)

    logger.info("MyGene.info enriched %d/%d genes", len(results), len(ensembl_ids))
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False) -> None:
    from app.database import get_supabase

    checkpoint = load_checkpoint(CHECKPOINT_NAME)
    processed_diseases: set[str] = set(checkpoint.get("processed", []))

    logger.info(
        "Starting gene population%s. Already processed: %d disease(s).",
        " [DRY RUN]" if dry_run else "",
        len(processed_diseases),
    )

    all_genes: dict[str, dict[str, Any]] = {}   # keyed by ensembl_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        ot_rate   = RateLimiter(calls_per_second=2.0)
        gene_rate = RateLimiter(calls_per_second=5.0)

        for category, diseases in DISEASE_CATEGORIES.items():
            for disease_id, disease_name in diseases:
                if disease_id in processed_diseases:
                    logger.info("Skipping %s (already processed)", disease_id)
                    continue

                await ot_rate.wait()
                genes = await fetch_genes_for_disease(
                    client, disease_id, disease_name, limit=GENES_PER_DISEASE
                )

                for g in genes:
                    eid = g["ensembl_id"]
                    if eid and eid not in all_genes:
                        all_genes[eid] = g

                checkpoint["processed"].append(disease_id)
                save_checkpoint(CHECKPOINT_NAME, checkpoint)

        # Enrich descriptions via MyGene.info
        ensembl_ids = list(all_genes.keys())
        logger.info("Enriching %d unique genes via MyGene.info …", len(ensembl_ids))
        enriched = await enrich_with_mygene(client, ensembl_ids, gene_rate)
        for eid, extra in enriched.items():
            if eid in all_genes:
                if extra.get("gene_name") and not all_genes[eid]["gene_name"]:
                    all_genes[eid]["gene_name"] = extra["gene_name"]
                if extra.get("description") and not all_genes[eid]["description"]:
                    all_genes[eid]["description"] = extra["description"]

    # Build insert records.
    # ensembl_id is not a column in the genes table, so we fold it into the
    # description field as "Ensembl: {id}. {description}" for later use by
    # the KEGG pathway script which needs it to resolve Entrez IDs.
    db_records = [
        {
            "gene_symbol": g["gene_symbol"],
            "gene_name":   g["gene_name"],
            "description": (
                f"Ensembl: {g['ensembl_id']}. {g['description']}".strip(". ")
                if g.get("ensembl_id")
                else g["description"]
            ),
        }
        for g in all_genes.values()
        if g.get("gene_symbol")
    ]

    logger.info("Collected %d unique gene(s) to insert.", len(db_records))

    if not dry_run:
        client_db = await get_supabase()
        existing_symbols = await get_existing_values(client_db, "genes", "gene_symbol")
        new_records = [r for r in db_records if r["gene_symbol"] not in existing_symbols]
        logger.info(
            "%d already in DB; inserting %d new gene(s).",
            len(existing_symbols),
            len(new_records),
        )
        inserted = await batch_insert(client_db, "genes", new_records, logger=logger)
        logger.info("Inserted %d gene rows.", inserted)
    else:
        logger.info("[DRY RUN] Would insert %d gene row(s).", len(db_records))

    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate genes table from Open Targets.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without inserting")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
